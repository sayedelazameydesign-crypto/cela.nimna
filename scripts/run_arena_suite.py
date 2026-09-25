"""Arena task suite — measure the agent itself, not just the diff (P0.5).

Ticket P0.5-T1 in ``docs/architecture/agent-platform-audit.md``:

    python scripts/run_arena_suite.py --mode mock

Runs every task in ``evals/tasks/*.yaml`` against the real ``Agent`` loop
(MockProvider by default), then checks a deterministic rubric per task:

* ``expected_artifacts`` — files that must exist (optionally contain a string)
* ``must_include`` / ``must_not_include`` — terms in the final reply
* secret scan over every file in the task workspace (reuses the
  ``SECRET_PATTERNS`` gate from ``scripts/evaluate_arena.py``)

Each run is recorded into a SQLite ledger (``evals/ledger/ledger.db``) so the
next run can flag regressions, and the suite renders a Markdown/JSON report.

Honesty contract (mirrors ``scripts/evaluate_arena.py``):

* ``--mode mock``  → every ran row is verdict ``MOCKED`` with a top banner;
  the score proves the pipeline works, never the agent's quality. ``PASS``
  is never printed in mock mode.
* ``--mode live`` with no provider key → rows ``SKIPPED`` (no fake numbers).
* provider/runner failure → ``ERROR`` row (banner), never silently dropped.
* tasks requiring disabled capabilities (network, shell_tool, docker) are
  ``SKIPPED`` with the reason.
* ``judge_rubric`` is stored per task now; the LLM-as-a-Judge scorer that
  consumes it lands in P5 — no judge score exists today and none is invented.

Dependencies: stdlib + ``yaml`` + the project package itself (this runner
imports ``nimna`` — unlike ``evaluate_arena.py`` it drives the real agent).

Exit codes: ``0`` report produced, ``1`` usage/IO/schema error,
``2`` ``--strict`` and the suite has an ERROR row, a secret hit, or a
regression against the ledger.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TASKS_DIR = REPO / "evals" / "tasks"
DEFAULT_LEDGER = REPO / "evals" / "ledger" / "ledger.db"

REPORT_VERSION = 1
MAX_SECRET_HITS = 20
MAX_SCAN_BYTES = 1_000_000
SCORE_EPSILON = 1e-9

CATEGORIES = {"coding", "web", "data", "arabic_long"}
TASK_REQUIRED_KEYS = {"id", "category", "prompt"}
TASK_KNOWN_KEYS = {
    "id", "title", "category", "prompt", "requires", "allowed_tools",
    "max_steps", "max_tool_calls", "timeout_s", "seed_files",
    "expected_artifacts", "must_include", "must_not_include", "judge_rubric",
    # mock-mode scripting: deterministic tool exercises for the pipeline lab
    "mock_skills", "mock_script", "mock_final",
    # P1-T3: declarative deterministic verification spec (no LLM)
    "verify",
}
VERDICT_ICON = {"PASS": "✅", "FAIL": "❌", "MOCKED": "🧪", "SKIPPED": "⏭️", "ERROR": "⚠️"}


class SuiteError(Exception):
    """Schema / IO problem that aborts the whole suite."""


# --------------------------------------------------------------------------- #
# Task schema
# --------------------------------------------------------------------------- #
@dataclass
class SeedFile:
    path: str
    content: str


@dataclass
class ExpectedArtifact:
    path: str
    contains: str | None = None


@dataclass
class TaskSpec:
    id: str
    category: str
    prompt: str
    title: str = ""
    requires: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = 12
    max_tool_calls: int = 30
    timeout_s: int = 180
    seed_files: list[SeedFile] = field(default_factory=list)
    expected_artifacts: list[ExpectedArtifact] = field(default_factory=list)
    must_include: list[str] = field(default_factory=list)
    must_not_include: list[str] = field(default_factory=list)
    judge_rubric: str = ""
    mock_skills: list[str] = field(default_factory=list)
    mock_script: list[dict[str, Any]] = field(default_factory=list)
    mock_final: str = ""
    verify: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""

    @classmethod
    def from_dict(cls, data: Any, source: str = "<memory>") -> TaskSpec:
        if not isinstance(data, dict):
            raise SuiteError(f"{source}: task must be a YAML mapping")
        missing = TASK_REQUIRED_KEYS - set(data)
        unknown = set(data) - TASK_KNOWN_KEYS
        if missing:
            raise SuiteError(f"{source}: missing required keys: {sorted(missing)}")
        if unknown:
            raise SuiteError(f"{source}: unknown keys: {sorted(unknown)}")
        task_id = str(data["id"]).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{2,63}", task_id):
            raise SuiteError(f"{source}: bad id {task_id!r} (lowercase/digits/dashes)")
        category = str(data["category"]).strip()
        if category not in CATEGORIES:
            raise SuiteError(f"{source}: category {category!r} not in {sorted(CATEGORIES)}")
        prompt = str(data["prompt"]).strip()
        if not prompt:
            raise SuiteError(f"{source}: empty prompt")
        max_steps = int(data.get("max_steps") or 12)
        if not 1 <= max_steps <= 60:
            raise SuiteError(f"{source}: max_steps out of range 1..60")
        timeout_s = int(data.get("timeout_s") or 180)
        if not 5 <= timeout_s <= 1800:
            raise SuiteError(f"{source}: timeout_s out of range 5..1800")
        seed_files = []
        for item in data.get("seed_files") or []:
            if not isinstance(item, dict) or "path" not in item or "content" not in item:
                raise SuiteError(f"{source}: seed_files entries need 'path' and 'content'")
            seed_files.append(SeedFile(path=str(item["path"]), content=str(item["content"])))
        artifacts = []
        for item in data.get("expected_artifacts") or []:
            if not isinstance(item, dict) or "path" not in item:
                raise SuiteError(f"{source}: expected_artifacts entries need 'path'")
            contains = item.get("contains")
            contains = str(contains) if contains else None
            artifacts.append(ExpectedArtifact(path=str(item["path"]), contains=contains))
        requires = [str(r) for r in (data.get("requires") or [])]
        known_requires = {"network", "shell_tool", "docker_sandbox"}
        bad = set(requires) - known_requires
        if bad:
            raise SuiteError(f"{source}: unknown requires {sorted(bad)} (known: {sorted(known_requires)})")
        try:
            from nimna.execution.verification import validate_spec
            verify_spec = validate_spec(data.get("verify") or [])
        except ImportError:
            verify_spec = list(data.get("verify") or [])
        except (ValueError, TypeError) as exc:
            raise SuiteError(f"{source}: invalid verify spec: {exc}") from None
        mock_script: list[dict[str, Any]] = []
        for step in (data.get("mock_script") or []):
            if not isinstance(step, dict) or not str(step.get("tool") or "").strip():
                raise SuiteError(f"{source}: mock_script steps need a 'tool' name")
            if len(mock_script) >= 12:
                raise SuiteError(f"{source}: mock_script is capped at 12 steps")
            mock_script.append({"tool": str(step["tool"]),
                                "arguments": dict(step.get("arguments") or {})})
        return cls(
            id=task_id, category=category, prompt=prompt,
            title=str(data.get("title") or task_id),
            requires=requires,
            allowed_tools=[str(t) for t in (data.get("allowed_tools") or [])],
            max_steps=max_steps,
            max_tool_calls=int(data.get("max_tool_calls") or 30),
            timeout_s=timeout_s,
            seed_files=seed_files, expected_artifacts=artifacts,
            must_include=[str(t) for t in (data.get("must_include") or [])],
            must_not_include=[str(t) for t in (data.get("must_not_include") or [])],
            judge_rubric=str(data.get("judge_rubric") or ""),
            mock_skills=[str(s) for s in (data.get("mock_skills") or [])],
            mock_script=mock_script,
            mock_final=str(data.get("mock_final") or ""),
            verify=verify_spec,
            source=source,
        )


def load_tasks(tasks_dir: Path) -> list[TaskSpec]:
    """Load and validate every ``*.yaml`` task; ids must be unique."""
    if not tasks_dir.is_dir():
        raise SuiteError(f"tasks dir not found: {tasks_dir}")
    specs: list[TaskSpec] = []
    seen: set[str] = set()
    for path in sorted(tasks_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise SuiteError(f"{path.name}: invalid YAML: {exc}") from exc
        spec = TaskSpec.from_dict(data, source=path.name)
        if spec.id in seen:
            raise SuiteError(f"{path.name}: duplicate task id {spec.id!r}")
        seen.add(spec.id)
        specs.append(spec)
    if not specs:
        raise SuiteError(f"no tasks found in {tasks_dir}")
    return specs


# --------------------------------------------------------------------------- #
# Ledger (SQLite) — the evolution memory of scores
# --------------------------------------------------------------------------- #
class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                 run_id TEXT PRIMARY KEY, ts TEXT NOT NULL, task_id TEXT NOT NULL,
                 mode TEXT NOT NULL, verdict TEXT NOT NULL, score REAL,
                 checks_passed INTEGER, checks_total INTEGER, model TEXT,
                 repo_version TEXT, details TEXT)"""
        )
        self._conn.commit()

    def record(self, row: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.get("run_id") or uuid.uuid4().hex[:16],
                row["ts"], row["task_id"], row["mode"], row["verdict"],
                row.get("score"), row.get("checks_passed"), row.get("checks_total"),
                row.get("model"), row.get("repo_version"),
                json.dumps(row.get("details") or {}, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def last_scores(self, mode: str) -> dict[str, float]:
        """Latest recorded score per task for *mode* (empty where score is NULL)."""
        rows = self._conn.execute(
            "SELECT task_id, score FROM runs WHERE mode=? AND score IS NOT NULL "
            "ORDER BY rowid ASC", (mode,),  # rowid = insertion order → latest wins
        ).fetchall()
        return {task_id: float(score) for task_id, score in rows}

    def close(self) -> None:
        self._conn.close()


def repo_version() -> str:
    import subprocess
    env_version = (os.getenv("ARENA_REPO_VERSION") or "").strip()
    if env_version:
        return env_version
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip() or "unknown"
    except Exception:
        pass
    return "unknown"


# --------------------------------------------------------------------------- #
# Static gate reuse — secret scanning from scripts/evaluate_arena.py
# --------------------------------------------------------------------------- #
_EV_CACHE = None


def _load_evaluate_arena():
    """Load (once) the static-gate module and reuse its secret patterns."""
    global _EV_CACHE
    if _EV_CACHE is not None:
        return _EV_CACHE
    import importlib.util
    path = REPO / "scripts" / "evaluate_arena.py"
    spec = importlib.util.spec_from_file_location("evaluate_arena_for_suite", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _EV_CACHE = module
    return module


def scan_secret_hits(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, pattern_name)`` for secret-like lines in *text*."""
    ev = _load_evaluate_arena()
    hits: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern in ev.SECRET_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            value = match.groupdict().get("value") if match.groupdict() else None
            if name == "generic-assignment" and ev._looks_like_placeholder(line, value):
                continue
            hits.append((line_no, name))
            break
    return hits


def _workspace_texts(workspace: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            continue
        try:
            texts[str(path.relative_to(workspace))] = path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    return texts


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def _safe_join(workspace: Path, rel: str) -> Path:
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or str(rel).strip() in {"", "."}:
        raise SuiteError(f"unsafe path outside workspace: {rel!r}")
    target = workspace.joinpath(*pure.parts)
    if target.is_symlink():
        raise SuiteError(f"refusing symlink path: {rel!r}")
    return target


def _capability_settings():
    from nimna.config import Settings
    return Settings.from_env(env_file=None)


def capability_map(settings: Any) -> dict[str, bool]:
    flag = {"1", "true", "yes", "on"}
    return {
        "network": (os.getenv("ARENA_SUITE_NETWORK") or "").strip().lower() in flag,
        "shell_tool": (os.getenv("SHELL_TOOL_ENABLED") or "").strip().lower() in flag,
        "docker_sandbox": getattr(settings, "sandbox_backend", "") == "docker",
    }


def _build_agent(mode: str, spec: TaskSpec, workspace: Path):
    from nimna.config import Settings
    from nimna.core.agent import Agent
    from nimna.core.approval import DeferToClient
    from nimna.memory import MemoryStore
    from nimna.providers import MockProvider
    from nimna.providers.base import ModelResponse, ToolCall
    from nimna.skills import SkillManager
    from nimna.tools import default_registry

    settings = Settings.from_env(env_file=None)
    settings.workspace_dir = workspace
    settings.skills_dir = REPO / "skills"
    settings.db_path = Path(":memory:")
    settings.max_steps = spec.max_steps
    settings.max_tool_calls = spec.max_tool_calls
    settings.max_runtime_seconds = spec.timeout_s
    settings.verify = False  # the suite measures the pipeline, not the verify pass
    if mode == "mock":
        settings.provider = "mock"
        settings.sandbox_backend = "subprocess"
        provider = MockProvider()
        if spec.mock_script or spec.mock_skills:
            # Scripted pipeline: skills selection -> one tool call per step -> final.
            import json as _json
            responses: list[Any] = [
                _json.dumps({"skills": list(spec.mock_skills), "reason": "suite-script", "plan": []})
            ]
            for step in spec.mock_script:
                responses.append(
                    ModelResponse(text="", tool_calls=[ToolCall(name=step["tool"], arguments=dict(step["arguments"]))])
                )
            responses.append(spec.mock_final or "تم تنفيذ المهمة.")
            provider.queue(*responses)
        # else: empty queue -> the descriptive default answer (legacy behaviour)
    else:
        from nimna.providers import create_provider
        provider = create_provider(settings)
    agent = Agent(
        provider, SkillManager(settings.skills_dir), default_registry(),
        MemoryStore(":memory:"), settings,
        approval_policy=DeferToClient(), workspace=workspace,
    )
    return agent, provider


def _last_shell_evidence(agent: Any, session_id: str) -> dict[str, Any] | None:
    """Last shell execution evidence from the SHA-256 audit chain: the delta
    payload, exit evidence, and the chain hash of the evidence event itself."""
    try:
        events = agent.memory.get_audit(session_id, limit=400)
    except Exception:
        return None
    for event in reversed(events):
        if event.get("event") != "shell_evidence":
            continue
        payload = event.get("payload") or {}
        changes = payload.get("filesystem_delta") or []
        chain = (payload.get("_evidence") or {}).get("hash") or ""
        return {
            "changes": changes,
            "snapshot_before": payload.get("snapshot_before") or "",
            "snapshot_after": payload.get("snapshot_after") or "",
            "before_root_hash": payload.get("before_root_hash") or "",
            "after_root_hash": payload.get("after_root_hash") or "",
            "summary": payload.get("delta_summary") or {},
            "exit_code": payload.get("exit_code"),
            "timed_out": bool(payload.get("timed_out")),
            "audit_hash": chain,
        }
    return None


def _run_checks(spec: TaskSpec, workspace: Path, reply: str,
                fs_delta: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    secret_hits: list[dict[str, Any]] = []

    for artifact in spec.expected_artifacts:
        name = f"artifact:{artifact.path}"
        try:
            target = _safe_join(workspace, artifact.path)
        except SuiteError as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})
            continue
        if not target.is_file():
            checks.append({"name": name, "ok": False, "detail": "missing"})
            continue
        if artifact.contains:
            content = target.read_text(encoding="utf-8", errors="replace")
            ok = artifact.contains in content
            checks.append({"name": name, "ok": ok,
                           "detail": "contains ok" if ok else f"missing text {artifact.contains!r}"})
        else:
            checks.append({"name": name, "ok": True, "detail": "exists"})

    for term in spec.must_include:
        ok = term in reply
        checks.append({"name": f"reply contains {term!r}", "ok": ok,
                       "detail": "ok" if ok else "term not in final reply"})
    for term in spec.must_not_include:
        ok = term not in reply
        checks.append({"name": f"reply excludes {term!r}", "ok": ok,
                       "detail": "ok" if ok else "forbidden term in final reply"})

    if fs_delta:
        # P1-T2 §10: expected artifacts proven by execution evidence —
        # observed in the delta AND re-verified by content hash
        # (Delta + Re-observation = Evidence; Delta alone is a claim).
        try:
            from nimna.execution.observation import FilesystemDelta, WorkspaceObserver
            delta = FilesystemDelta.from_payload(fs_delta)
            observer = WorkspaceObserver(workspace)
            verify = {r["path"]: r for r in observer.verify_delta(delta)}
            observed = {c["path"]: c for c in fs_delta.get("changes") or []
                        if c.get("kind") in {"CREATED", "MODIFIED", "RENAMED"}}
            for artifact in spec.expected_artifacts:
                change = observed.get(artifact.path)
                if change is None:
                    continue  # exists/contains check already covers absence
                checks.append({"name": f"delta:{artifact.path} observed {change['kind']}",
                               "ok": True, "detail": "from shell execution evidence"})
                result = verify.get(artifact.path) or {}
                checks.append({"name": f"delta:{artifact.path} sha re-verified",
                               "ok": bool(result.get("match")),
                               "detail": result.get("note") or "not re-verified"})
        except Exception as exc:
            checks.append({"name": "delta:evidence", "ok": False,
                           "detail": f"delta verification failed: {exc.__class__.__name__}"})

    for rel, content in _workspace_texts(workspace).items():
        for line_no, pattern_name in scan_secret_hits(content):
            secret_hits.append({"file": rel, "line": line_no, "pattern": pattern_name})
            if len(secret_hits) >= MAX_SECRET_HITS:
                break
        if len(secret_hits) >= MAX_SECRET_HITS:
            break
    if secret_hits:
        checks.append({"name": "secret-scan", "ok": False,
                       "detail": f"{len(secret_hits)} secret-like hit(s); values are never echoed"})
    return checks, secret_hits


def _now_perf() -> float:
    import time
    return time.perf_counter()


_SHELL_EVIDENCE_KEYS = {"tool", "command_hash", "cwd", "policy", "authorization",
                        "started_at", "duration_ms", "exit_code", "stdout_hash",
                        "stderr_hash", "filesystem_delta", "status"}


def _shell_metrics(agent: Any, session_id: str, result: Any, row: dict[str, Any], wall_ms: int) -> dict[str, Any]:
    """Before/after metrics (P1 contract §8) — derived from the real audit trail.

    completed/verified derive from deterministic checks; security_denials and
    evidence completeness come from shell_denied/shell_evidence audit events;
    recovered = the task hit a failure yet still finished with a perfect score.
    """
    try:
        events = agent.memory.get_audit(session_id, limit=400)
    except Exception:
        events = []
    shell_exec = [e for e in events if e.get("event") == "shell_evidence"]
    shell_denied = [e for e in events if e.get("event") == "shell_denied"]
    complete = sum(1 for e in shell_exec if _SHELL_EVIDENCE_KEYS <= set(e.get("payload") or {}))
    failed_shell = sum(1 for e in shell_exec
                       if (e.get("payload") or {}).get("status") in {"NONZERO_EXIT", "TIMEOUT", "SANDBOX_ERROR"})
    tool_calls = list(getattr(result, "tool_calls", []) or [])
    failed_calls = sum(1 for c in tool_calls if not getattr(c, "ok", True))
    score = row.get("score")
    ran_ok = row["verdict"] in {"MOCKED", "PASS"}
    return {
        "wall_ms": wall_ms,
        "tool_calls": len(tool_calls),
        "failed_tool_calls": failed_calls,
        "failed_shell_commands": failed_shell,
        "shell_executions": len(shell_exec),
        "shell_denials": len(shell_denied),
        "evidence_complete": complete,
        "evidence_completeness": round(100.0 * complete / len(shell_exec), 1) if shell_exec else None,
        "recovered": bool(ran_ok and score == 100.0 and (failed_calls or shell_denied or failed_shell)),
        "verified": bool(ran_ok and score == 100.0),
    }


def run_task(spec: TaskSpec, mode: str) -> dict[str, Any]:
    """Run one task end-to-end and return an honest row dict."""
    row: dict[str, Any] = {
        "task_id": spec.id, "category": spec.category, "title": spec.title,
        "mode": mode, "verdict": None, "score": None, "checks_passed": None,
        "checks_total": None, "checks": [], "secret_hits": [], "model": None,
        "status_detail": "", "tools_called": [], "skills_used": [],
        "requires": spec.requires,
        "metrics": {},
    }
    try:
        from nimna.core.state import RunStatus
    except Exception as exc:  # pragma: no cover - only if the package is broken
        row["verdict"] = "ERROR"
        row["status_detail"] = f"cannot import nimna: {exc.__class__.__name__}"
        return row

    if mode not in {"mock", "live"}:
        row["verdict"] = "ERROR"
        row["status_detail"] = f"unknown mode {mode!r}"
        return row

    try:
        capabilities = capability_map(_capability_settings())
    except Exception as exc:
        row["verdict"] = "ERROR"
        row["status_detail"] = f"cannot read settings: {exc.__class__.__name__}"
        return row
    missing = [r for r in spec.requires if not capabilities.get(r)]
    if missing:
        row["verdict"] = "SKIPPED"
        row["status_detail"] = "missing capabilities: " + ", ".join(sorted(missing))
        return row

    import shutil
    import tempfile
    workspace = Path(tempfile.mkdtemp(prefix=f"arena-{spec.id}-"))
    try:
        for seed in spec.seed_files:
            target = _safe_join(workspace, seed.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(seed.content, encoding="utf-8")

        try:
            agent, provider = _build_agent(mode, spec, workspace)
        except Exception as exc:
            from nimna.providers.base import ProviderError
            if isinstance(exc, ProviderError):
                row["verdict"] = "SKIPPED"
                row["status_detail"] = f"no live provider configured: {str(exc)[:150]}"
            else:
                row["verdict"] = "ERROR"
                row["status_detail"] = f"agent setup failed: {exc.__class__.__name__}: {str(exc)[:200]}"
            return row
        row["model"] = getattr(provider, "model", None) or getattr(provider, "name", None)

        started = _now_perf()
        try:
            result = agent.run(spec.prompt, session_id=f"arena-{spec.id}")
            # The suite is the lab operator: it grants pending approvals (bounded),
            # which exercises the real suspend/resume machinery deterministically.
            resumes = 0
            while getattr(result, "status", None) == RunStatus.AWAITING_APPROVAL and resumes <= len(spec.mock_script) + 2:
                result = agent.resume(result.run_id, True)
                resumes += 1
        except Exception as exc:
            row["verdict"] = "ERROR"
            row["status_detail"] = f"agent crashed: {exc.__class__.__name__}: {str(exc)[:200]}"
            return row
        wall_ms = int((_now_perf() - started) * 1000)

        if getattr(result, "status", None) == RunStatus.AWAITING_APPROVAL:
            row["verdict"] = "ERROR"
            row["status_detail"] = "run suspended awaiting approval (not supported inside the suite)"
            return row

        reply = getattr(result, "reply", "") or ""
        row["tools_called"] = [c.name for c in getattr(result, "tool_calls", []) if getattr(c, "name", None)]
        row["skills_used"] = list(getattr(result, "skills_used", []) or [])
        shell_ev = _last_shell_evidence(agent, f"arena-{spec.id}")
        checks, secret_hits = _run_checks(spec, workspace, reply, fs_delta=shell_ev)
        row["checks"] = checks
        row["secret_hits"] = secret_hits
        row["checks_total"] = len(checks)
        row["checks_passed"] = sum(1 for c in checks if c["ok"])
        row["score"] = round(100.0 * row["checks_passed"] / row["checks_total"], 1) if checks else None
        # P1-T3: deterministic verifier (not an LLM) over the execution evidence
        if spec.verify:
            try:
                from nimna.execution.verification import DeterministicVerifier
                if shell_ev:
                    row["fs_evidence"] = {
                        "before_root_hash": shell_ev["before_root_hash"],
                        "after_root_hash": shell_ev["after_root_hash"],
                        "delta_changes": len(shell_ev["changes"]),
                        "audit_hash": shell_ev["audit_hash"],
                    }

                # P1-T5: command re-runs go through the gated Tool Registry —
                # schema → capability → policy → authorization → execute → evidence.
                from nimna.config import Settings as _S
                from nimna.execution.tool_registry import (
                    EvidenceChain,
                    InvocationStatus,
                    ToolDescriptor,
                    ToolRegistry,
                )
                from nimna.tools.builtin.shell import ShellRequest as _Req
                from nimna.tools.builtin.shell import execute_shell as _exe

                _settings = _S.from_env(env_file=None)
                _settings.shell_tool_enabled = capabilities.get("shell_tool", False)
                _settings.sandbox_memory_mb = 256
                _shell_granted = bool(capabilities.get("shell_tool"))
                registry = ToolRegistry()
                registry_evidence = EvidenceChain()

                def _sandbox_command(arguments: dict) -> dict:
                    res = _exe(_Req(command=arguments["command"],
                                    timeout_ms=max(100, min(int(arguments["timeout_ms"]), 60_000))),
                               settings=_settings, workspace_root=workspace, explicit_consent=True)
                    status = res.status.value if hasattr(res.status, "value") else str(res.status)
                    return {"status": str(status), "exit_code": int(res.exit_code or 0),
                            "timed_out": bool(res.timed_out)}

                registry.register(ToolDescriptor(
                    tool_id="sandbox.command", version="1.0.0",
                    input_schema={"type": "object",
                                  "properties": {"command": {"type": "string", "maxLength": 4000},
                                                 "timeout_ms": {"type": "integer"}},
                                  "required": ["command"], "additionalProperties": False},
                    output_schema={"type": "object",
                                   "properties": {"status": {"type": "string"},
                                                  "exit_code": {"type": "integer"},
                                                  "timed_out": {"type": "boolean"}},
                                   "required": ["status"]},
                    capabilities=("shell",), side_effects=("process", "filesystem"),
                    risk_level="HIGH", handler=_sandbox_command))

                # P1-T6: deterministic governance — CapabilityCatalog → Policy →
                # Authorization. Same inputs, same decision; every refusal fail-closed.
                from nimna.execution.policy import (
                    AuthorizationGrant,
                    Authorizer,
                    CapabilityCatalog,
                    Effect,
                    Policy,
                    PolicyRule,
                )
                catalog = CapabilityCatalog(known={"shell"})
                suite_policy = Policy("arena-suite-policy", "1.0.0", rules=(
                    PolicyRule("deny-outside-workspace", Effect.DENY,
                               capabilities=frozenset({"shell"}), operation="*",
                               resource_patterns=("system/*", "/etc/*", "/*"),
                               note="no verification command may leave the workspace"),
                    PolicyRule("allow-sandbox-shell", Effect.ALLOW,
                               capabilities=frozenset({"shell"}), operation="*",
                               resource_patterns=("workspace", "workspace/*", "workspace/**"),
                               note="sandboxed verification re-run inside the workspace"),
                ))
                suite_authorizer = Authorizer()
                policy_stats: dict = {}  # noqa: F841  # TODO(decision-D): تجربة السلك جارية — قرار معلّق (سجل الدفعة 4 في تقرير التدقيق)

                def _operator_consent(descriptor, arguments):
                    return AuthorizationGrant(actor="arena-suite-operator",
                                              tool_id=descriptor.tool_id,
                                              policy_version=suite_policy.version)

                # P1-T7: the suite crosses the fabric through ONE gateway —
                # Registry → Capability → Policy → Authorization → Execute →
                # Observe → Verify → Checkpoint → Evidence.
                from nimna.execution.gateway import ExecutionGateway, InvocationContext
                from nimna.execution.recovery import CheckpointStore
                gateway = ExecutionGateway(
                    registry, catalog=catalog, policy=suite_policy,
                    authorizer=suite_authorizer, workspace_root=workspace,
                    checkpoint_store=CheckpointStore(
                        Path(tempfile.mkdtemp(prefix=f"arena-{spec.id}-gw-"))),
                    evidence=registry_evidence, actor="arena-suite-operator")
                gateway_outcomes: list = []

                def _exec_fn(command: str, timeout_ms: int):
                    context = InvocationContext(
                        actor="arena-suite-operator", session_id=f"arena-{spec.id}",
                        mission_id=f"arena-{spec.id}:verify",
                        granted_capabilities=("shell",) if _shell_granted else (),
                        authorization=(_operator_consent(
                            registry.get("sandbox.command"), {})
                            if _shell_granted else None),
                        requested_operation="verify.command", resource="workspace",
                        verify_spec=({"kind": "exit_code", "equals": 0},))
                    outcome = gateway.invoke(
                        "sandbox.command",
                        {"command": command, "timeout_ms": int(timeout_ms)}, context)
                    gateway_outcomes.append(outcome)
                    if not outcome.handler_called:
                        # refused before the handler ⇒ honest INCONCLUSIVE downstream
                        return {"status": "DENIED"}
                    return outcome.result
                    if outcome.status is not InvocationStatus.EXECUTED:
                        # refused ⇒ honest INCONCLUSIVE downstream, never a fake PASS
                        return {"status": "DENIED"}
                    return outcome.result

                verifier = DeterministicVerifier(workspace)
                report_v = verifier.verify(
                    spec.verify,
                    shell_exit_code=shell_ev["exit_code"] if shell_ev else None,
                    shell_timed_out=shell_ev["timed_out"] if shell_ev else False,
                    fs_delta=shell_ev,
                    exec_fn=_exec_fn,
                )
                vdata = report_v.to_dict()
                row["verification"] = vdata
                _gstats = gateway.policy_stats
                row["policy"] = {"policy_id": suite_policy.policy_id,
                                 "version": suite_policy.version,
                                 "allow": _gstats.get("ALLOW", 0),
                                 "deny": _gstats.get("DENY", 0),
                                 "require_confirmation": _gstats.get("REQUIRE_CONFIRMATION", 0)}
                row["gateway"] = {
                    "invoked": sum(1 for o in gateway_outcomes if o.handler_called),
                    "refused": sum(1 for o in gateway_outcomes if not o.handler_called),
                    "observed": sum(1 for o in gateway_outcomes if o.observation),
                    "verified": sum(1 for o in gateway_outcomes if o.verification),
                    "checkpointed": sum(1 for o in gateway_outcomes if o.checkpoint),
                    "evidence_tail": gateway.evidence.tail[7:23],
                    "chain_verified": gateway.evidence.verify(),
                }
                row["registry"] = {
                    "registered": [tool.tool_id for tool in registry.list()],
                    "invocations": len(registry_evidence),
                    "authorized": sum(1 for e in registry_evidence.entries if e["decision"] == "EXECUTED"),
                    "denied": sum(1 for e in registry_evidence.entries if e["decision"] in {
                        "NOT_FOUND", "DISABLED", "REVOKED", "CAPABILITY_DENIED",
                        "POLICY_DENIED", "AUTHORIZATION_DENIED"}),
                    "revoked": sum(1 for e in registry_evidence.entries if e["decision"] == "REVOKED"),
                    "schema_failures": sum(1 for e in registry_evidence.entries if e["decision"] in {
                        "INPUT_INVALID", "OUTPUT_VIOLATION"}),
                    "evidence_tail": registry_evidence.tail[7:23],
                    "chain_verified": registry_evidence.verify(),
                }
                checks.append({"name": f"verifier:{vdata['verdict']}",
                               "ok": vdata["verdict"] == "PASS",
                               "detail": f"passed {vdata['summary']['passed']}/{vdata['summary']['total']}"
                                         f" · failed {vdata['summary']['failed']}"
                                         f" · inconclusive {vdata['summary']['inconclusive']}"})
                row["checks"] = checks
                row["checks_total"] = len(checks)
                row["checks_passed"] = sum(1 for c in checks if c["ok"])
                row["score"] = round(100.0 * row["checks_passed"] / row["checks_total"], 1) if checks else None
            except Exception as exc:
                row["verification"] = {"verdict": "INCONCLUSIVE",
                                       "error": f"{exc.__class__.__name__}: {str(exc)[:150]}"}
                row["registry"] = {"registered": [tool.tool_id for tool in registry.list()],
                                   "invocations": len(registry_evidence),
                                   "evidence_tail": registry_evidence.tail[7:23],
                                   "chain_verified": registry_evidence.verify(),
                                   "error": f"verifier crashed: {exc.__class__.__name__}"}
                row["policy"] = {"policy_id": "arena-suite-policy", "version": "1.0.0",
                                 "allow": gateway.policy_stats.get("ALLOW", 0),
                                 "deny": gateway.policy_stats.get("DENY", 0),
                                 "require_confirmation": gateway.policy_stats.get("REQUIRE_CONFIRMATION", 0)}
                checks.append({"name": "verifier:INCONCLUSIVE", "ok": False,
                               "detail": f"verifier crashed: {exc.__class__.__name__}"})
                row["checks"] = checks
                row["checks_total"] = len(checks)
                row["checks_passed"] = sum(1 for c in checks if c["ok"])
                row["score"] = round(100.0 * row["checks_passed"] / row["checks_total"], 1) if checks else None

        # P1-T4: checkpoint the run's terminal outcome. Bounded: exactly one
        # checkpoint per run; the store lives OUTSIDE the observed workspace so
        # atomic persistence never perturbs the filesystem evidence.
        if shell_ev:
            try:
                from nimna.execution.recovery import CheckpointStore, RecoveryManager
                mission = f"arena-{spec.id}"
                ckpt_dir = Path(tempfile.mkdtemp(prefix=f"arena-{spec.id}-ckpt-"))
                ckpt_manager = RecoveryManager(CheckpointStore(ckpt_dir))
                ckpt_manager.register(mission)
                verdict_v = (row.get("verification") or {}).get("verdict")
                cp = ckpt_manager.checkpoint(
                    mission, step_id="verify",
                    observation_fingerprint=shell_ev["after_root_hash"],
                    evidence_head=shell_ev["audit_hash"],
                    authorization_state={"granted": bool(capabilities.get("shell_tool")),
                                         "expires_at": None},
                    execution_state={"attempts": [{
                        "step_id": "run", "action_hash": shell_ev["audit_hash"],
                        "status": "completed", "verified": verdict_v == "PASS",
                        "evidence_hash": shell_ev["audit_hash"]}]})
                if verdict_v == "PASS":
                    ckpt_manager.complete(mission, "deterministic verifier PASS")
                elif verdict_v == "FAIL":
                    ckpt_manager.fail(mission, "deterministic verifier FAIL")
                # else: INCONCLUSIVE / no verify spec ⇒ stays CHECKPOINTED —
                # diagnosable, never claimed complete without a PASS verdict.
                row["recovery"] = {
                    "state": ckpt_manager.state(mission).value,
                    "checkpoint_id": cp.checkpoint_id,
                    "state_version": cp.state_version,
                    "evidence_head": cp.evidence_head,
                    "observation_fingerprint": cp.observation_fingerprint,
                }
            except Exception as exc:
                row["recovery"] = {"state": "RECOVERY_ERROR",
                                   "error": f"{exc.__class__.__name__}: {str(exc)[:120]}"}

        if mode == "mock":
            row["verdict"] = "MOCKED"  # never PASS: the pipeline was exercised, not the quality
            row["status_detail"] = "mock provider run — deterministic checks only"
        else:
            row["verdict"] = "PASS" if not secret_hits and row["checks_passed"] == row["checks_total"] else "FAIL"
            row["status_detail"] = "live run — deterministic checks only (LLM judge arrives in P5)"
        row["metrics"] = _shell_metrics(agent, f"arena-{spec.id}", result, row, wall_ms)
        row["metrics"]["delta_reverified"] = sum(
            1 for c in checks if c["name"].endswith("sha re-verified") and c["ok"])
        return row
    except SuiteError as exc:
        row["verdict"] = "ERROR"
        row["status_detail"] = str(exc)
        return row
    except Exception as exc:  # noqa: BLE001 - the suite must report, not crash
        row["verdict"] = "ERROR"
        row["status_detail"] = f"runner error: {exc.__class__.__name__}: {str(exc)[:200]}"
        if os.getenv("ARENA_SUITE_DEBUG"):
            row["status_detail"] += "\n" + traceback.format_exc(limit=3)
        return row
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def run_suite(tasks: list[TaskSpec], mode: str, *, ledger_path: Path | None = DEFAULT_LEDGER) -> dict[str, Any]:
    previous: dict[str, float] = {}
    ledger = None
    if ledger_path is not None:
        ledger = Ledger(ledger_path)
        previous = ledger.last_scores(mode)

    rows: list[dict[str, Any]] = []
    for spec in tasks:
        row = run_task(spec, mode)
        row["run_id"] = uuid.uuid4().hex[:16]
        row["ts"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        row["repo_version"] = repo_version()
        if ledger is not None:
            ledger.record(row)
        row["previous_score"] = previous.get(spec.id)
        rows.append(row)

    if ledger is not None:
        ledger.close()

    regressions = [
        {"task_id": r["task_id"], "previous_score": r["previous_score"], "score": r["score"]}
        for r in rows
        if r.get("previous_score") is not None and r.get("score") is not None
        and r["score"] < r["previous_score"] - SCORE_EPSILON
    ]
    scores = [r["score"] for r in rows if r["score"] is not None]
    metrics = [r["metrics"] for r in rows if r.get("metrics")]
    shell_exec_total = sum(m["shell_executions"] for m in metrics)
    ev_total = sum(m["evidence_complete"] for m in metrics)
    walls = [m["wall_ms"] for m in metrics if m["wall_ms"]]
    return {
        "version": REPORT_VERSION,
        "mode": mode,
        "repo_version": repo_version(),
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "rows": rows,
        "summary": {
            "tasks": len(rows),
            "ran": sum(1 for r in rows if r["verdict"] in {"MOCKED", "PASS", "FAIL"}),
            "skipped": sum(1 for r in rows if r["verdict"] == "SKIPPED"),
            "error": sum(1 for r in rows if r["verdict"] == "ERROR"),
            "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
            "secret_hits": sum(len(r.get("secret_hits") or []) for r in rows),
            # before/after metrics (P1 contract §8)
            "verified": sum(1 for m in metrics if m["verified"]),
            "tool_calls": sum(m["tool_calls"] for m in metrics),
            "failed_tool_calls": sum(m["failed_tool_calls"] for m in metrics),
            "failed_commands": sum(m["failed_shell_commands"] for m in metrics),
            "security_denials": sum(m["shell_denials"] for m in metrics),
            "shell_executions": shell_exec_total,
            "evidence_completeness": round(100.0 * ev_total / shell_exec_total, 1) if shell_exec_total else None,
            "recovered_tasks": sum(1 for m in metrics if m["recovered"]),
            "mean_wall_ms": round(sum(walls) / len(walls), 1) if walls else None,
            "delta_reverified": sum(int(m.get("delta_reverified") or 0) for m in metrics),
            "verifier_pass": sum(1 for r in rows if (r.get("verification") or {}).get("verdict") == "PASS"),
            "verifier_fail": sum(1 for r in rows if (r.get("verification") or {}).get("verdict") == "FAIL"),
            "verifier_inconclusive": sum(1 for r in rows if (r.get("verification") or {}).get("verdict") == "INCONCLUSIVE"),
            "checkpoint_completed": sum(1 for r in rows if (r.get("recovery") or {}).get("state") == "COMPLETED"),
            "checkpoint_failed": sum(1 for r in rows if (r.get("recovery") or {}).get("state") == "FAILED"),
            "checkpoint_diagnosable": sum(1 for r in rows if (r.get("recovery") or {}).get("state") == "CHECKPOINTED"),
            "registry_registered": sum(len((r.get("registry") or {}).get("registered") or []) for r in rows),
            "registry_authorized": sum(int((r.get("registry") or {}).get("authorized") or 0) for r in rows),
            "registry_denied": sum(int((r.get("registry") or {}).get("denied") or 0) for r in rows),
            "registry_revoked": sum(int((r.get("registry") or {}).get("revoked") or 0) for r in rows),
            "registry_schema_failures": sum(int((r.get("registry") or {}).get("schema_failures") or 0) for r in rows),
            "policy_allow": sum(int((r.get("policy") or {}).get("allow") or 0) for r in rows),
            "policy_deny": sum(int((r.get("policy") or {}).get("deny") or 0) for r in rows),
            "policy_require_confirmation": sum(int((r.get("policy") or {}).get("require_confirmation") or 0) for r in rows),
            "gateway_invoked": sum(int((r.get("gateway") or {}).get("invoked") or 0) for r in rows),
            "gateway_refused": sum(int((r.get("gateway") or {}).get("refused") or 0) for r in rows),
        },
        "regressions": regressions,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_markdown(report: dict[str, Any]) -> str:
    mode = report["mode"]
    summary = report["summary"]
    lines: list[str] = [f"### 🧪 Arena Task Suite — mode: `{mode}`", ""]
    # Banner first: a MOCKED run can never be skimmed as a real quality signal.
    if mode == "mock":
        lines += [
            "> ⚠️ **MOCKED SUITE — ليست نتيجة جودة حقيقية.** كل المهام شُغِّلت على `MockProvider`؛ "
            "النتيجة تثبت أن خط القياس يعمل فقط. الحكم الحقيقي يأتي في P5 (live + judge). "
            "لا يُطبع PASS في هذا الوضع أبداً.",
            "",
        ]
    else:
        lines += [
            "> ℹ️ **LIVE MODE — فحوص حتمية فقط** (artifacts + keywords + أسرار). "
            "حكم الـ LLM-as-a-Judge على الـ rubric يُضاف في P5.",
            "",
        ]
    shell_flag = (os.getenv("SHELL_TOOL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"})
    lines.append(
        f"**Repo:** `{report['repo_version']}` · **Tasks:** `{summary['tasks']}` · "
        f"**Generated:** `{report['generated_at']}` · **run_command:** `{'enabled' if shell_flag else 'disabled'}`"
    )
    lines.append("")
    lines += [
        "| Task | Category | Verdict | Check score | Checks | Notes |",
        "| :--- | :--- | :--- | ---: | ---: | :--- |",
    ]
    for row in report["rows"]:
        verdict = row["verdict"] or "?"
        score = "—" if row.get("score") is None else f"{row['score']}"
        checks = "—" if row.get("checks_total") is None else f"{row['checks_passed']}/{row['checks_total']}"
        failed = [c["name"] for c in row.get("checks") or [] if not c["ok"]]
        notes: list[str] = [row.get("status_detail") or ""]
        if failed:
            notes.append("failed: " + ", ".join(failed[:4]))
        if row.get("secret_hits"):
            notes.append(f"🚨 {len(row['secret_hits'])} secret-like hit(s)")
        if row.get("verification"):
            notes.append(f"verifier {row['verification'].get('verdict')}")
        fs = row.get("fs_evidence")
        if fs:
            notes.append(
                f"fs: Δ{fs['delta_changes']} {str(fs['before_root_hash'])[7:19]}→{str(fs['after_root_hash'])[7:19]}"
                f" ev:{str(fs['audit_hash'])[7:23]}"
            )
        rec = row.get("recovery")
        if rec:
            notes.append(f"ckpt {rec['state']}:{str(rec.get('checkpoint_id', ''))[5:17]}")
        reg_row = row.get("registry")
        if reg_row:
            notes.append(f"reg: {reg_row.get('authorized', 0)}✓/{reg_row.get('denied', 0)}✗"
                         f" ev:{reg_row.get('evidence_tail', '—')}")
        if row.get("previous_score") is not None and row.get("score") is not None:
            delta = round(row["score"] - row["previous_score"], 1)
            notes.append(f"prev {row['previous_score']} ({'+' if delta >= 0 else ''}{delta})")
        note = " · ".join(n for n in notes if n).replace("|", "\\|")[:300]
        lines.append(
            f"| `{row['task_id']}` | {row['category']} | {VERDICT_ICON.get(verdict, '❔')} **{verdict}** "
            f"| {score} | {checks} | {note} |"
        )
    lines.append("")
    lines += [
        f"**Summary:** ran `{summary['ran']}` · skipped `{summary['skipped']}` · "
        f"error `{summary['error']}` · mean check-score `{summary['mean_score']}` · "
        f"secret hits `{summary['secret_hits']}` · regressions `{len(report['regressions'])}`",
        "",
        f"**Metrics:** verified `{summary.get('verified', '—')}/{summary['ran']}` · "
        f"tool calls `{summary.get('tool_calls', '—')}` (failed `{summary.get('failed_tool_calls', '—')}`) · "
        f"shell executions `{summary.get('shell_executions', '—')}` (failed commands `{summary.get('failed_commands', '—')}`) · "
        f"security denials `{summary.get('security_denials', '—')}` · "
        f"evidence completeness `{summary.get('evidence_completeness', '—')}`% · "
        f"recovered `{summary.get('recovered_tasks', '—')}` · mean wall `{summary.get('mean_wall_ms', '—')}`ms",
        "",
    ]
    if summary.get("delta_reverified"):
        lines += [
            f"**Delta evidence:** `{summary['delta_reverified']}` artifact hash(es) re-verified against "
            "execution evidence — Delta + Re-observation = Evidence (P1-T2).",
            "",
        ]
    if any((r.get("verification") or {}).get("verdict") for r in report["rows"]):
        lines += [
            f"**Verifier (P1-T3, deterministic — no LLM):** PASS `{summary.get('verifier_pass', 0)}` · "
            f"FAIL `{summary.get('verifier_fail', 0)}` · INCONCLUSIVE `{summary.get('verifier_inconclusive', 0)}`",
            "",
        ]
    if any(r.get("recovery") for r in report["rows"]):
        lines += [
            f"**Checkpoint (P1-T4, atomic store):** COMPLETED `{summary.get('checkpoint_completed', 0)}` · "
            f"FAILED `{summary.get('checkpoint_failed', 0)}` · diagnosable CHECKPOINTED `{summary.get('checkpoint_diagnosable', 0)}`",
            "",
        ]
    if any(r.get("registry") for r in report["rows"]):
        lines += [
            f"**Registry (P1-T5, gated invocation):** registered `{summary.get('registry_registered', 0)}` · "
            f"authorized `{summary.get('registry_authorized', 0)}` · denied `{summary.get('registry_denied', 0)}` · "
            f"revoked `{summary.get('registry_revoked', 0)}` · schema failures `{summary.get('registry_schema_failures', 0)}`",
            "",
        ]
    if any(r.get("policy") for r in report["rows"]):
        lines += [
            f"**Policy (P1-T6, deterministic governance):** ALLOW `{summary.get('policy_allow', 0)}` · "
            f"DENY `{summary.get('policy_deny', 0)}` · REQUIRE_CONFIRMATION `{summary.get('policy_require_confirmation', 0)}` · "
            "no-rule ⇒ default-DENY · policy error ⇒ DENY (fail-closed)",
            "",
        ]
    if any(r.get("gateway") for r in report["rows"]):
        lines += [
            f"**Gateway (P1-T7, single path):** invoked `{summary.get('gateway_invoked', 0)}` · "
            f"refused `{summary.get('gateway_refused', 0)}` — DENY ⇒ handler never called",
            "",
        ]
    if report["regressions"]:
        lines.append("**⚠️ Regressions vs ledger:**")
        for reg in report["regressions"]:
            lines.append(f"- `{reg['task_id']}`: {reg['previous_score']} → {reg['score']}")
        lines.append("")
    lines += [
        "> قاعدة الأمانة: `MOCKED` لا يعني نجاحاً و`SKIPPED` لا يعني فشلاً — "
        "والحكم الحقيقي على الجودة يُقاس في وضع live مع الحَكَم (P5).",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Arena agent task suite and record scores.")
    parser.add_argument("--mode", choices=("mock", "live"), default="mock",
                        help="mock = MockProvider (MOCKED verdicts, banner); live = configured provider")
    parser.add_argument("--tasks-dir", dest="tasks_dir", default=str(DEFAULT_TASKS_DIR))
    parser.add_argument("--task", help="run a single task by id")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="SQLite ledger path ('' disables recording)")
    parser.add_argument("--output", help="write the Markdown report here (default: stdout)")
    parser.add_argument("--json-output", dest="json_output", help="also write the JSON report here")
    parser.add_argument("--strict", action="store_true",
                        help="exit 2 on any ERROR row, secret hit, or ledger regression")
    args = parser.parse_args(argv)

    try:
        tasks = load_tasks(Path(args.tasks_dir))
    except SuiteError as exc:
        print(f"arena-suite: {exc}", file=sys.stderr)
        return 1
    if args.task:
        tasks = [t for t in tasks if t.id == args.task]
        if not tasks:
            print(f"arena-suite: no task with id {args.task!r}", file=sys.stderr)
            return 1

    ledger_path = Path(args.ledger) if args.ledger else None
    report = run_suite(tasks, args.mode, ledger_path=ledger_path)
    markdown = render_markdown(report)
    json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    if args.json_output:
        Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_output).write_text(json_text, encoding="utf-8")

    summary = report["summary"]
    print(
        f"arena-suite: ran={summary['ran']} skipped={summary['skipped']} "
        f"error={summary['error']} mean_score={summary['mean_score']} "
        f"secret_hits={summary['secret_hits']} regressions={len(report['regressions'])}",
        file=sys.stderr,
    )

    if args.strict and (summary["error"] or summary["secret_hits"] or report["regressions"]):
        print("arena-suite: strict mode found ERROR rows, secret hits, or regressions", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
