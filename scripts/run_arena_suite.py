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
    source: str = ""

    @classmethod
    def from_dict(cls, data: Any, source: str = "<memory>") -> "TaskSpec":
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
    from nimna.skills import SkillManager
    from nimna.tools import default_registry

    settings = Settings.from_env(env_file=None)
    settings.workspace_dir = workspace
    settings.skills_dir = REPO / "skills"
    settings.db_path = Path(":memory:")
    settings.max_steps = spec.max_steps
    settings.max_tool_calls = spec.max_tool_calls
    settings.max_runtime_seconds = spec.timeout_s
    if mode == "mock":
        settings.provider = "mock"
        settings.sandbox_backend = "subprocess"
        provider = MockProvider()
    else:
        from nimna.providers import create_provider
        provider = create_provider(settings)
    agent = Agent(
        provider, SkillManager(settings.skills_dir), default_registry(),
        MemoryStore(":memory:"), settings,
        approval_policy=DeferToClient(), workspace=workspace,
    )
    return agent, provider


def _run_checks(spec: TaskSpec, workspace: Path, reply: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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


def run_task(spec: TaskSpec, mode: str) -> dict[str, Any]:
    """Run one task end-to-end and return an honest row dict."""
    row: dict[str, Any] = {
        "task_id": spec.id, "category": spec.category, "title": spec.title,
        "mode": mode, "verdict": None, "score": None, "checks_passed": None,
        "checks_total": None, "checks": [], "secret_hits": [], "model": None,
        "status_detail": "", "tools_called": [], "skills_used": [],
        "requires": spec.requires,
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

        try:
            result = agent.run(spec.prompt, session_id=f"arena-{spec.id}")
        except Exception as exc:
            row["verdict"] = "ERROR"
            row["status_detail"] = f"agent crashed: {exc.__class__.__name__}: {str(exc)[:200]}"
            return row

        if getattr(result, "status", None) == RunStatus.AWAITING_APPROVAL:
            row["verdict"] = "ERROR"
            row["status_detail"] = "run suspended awaiting approval (not supported inside the suite)"
            return row

        reply = getattr(result, "reply", "") or ""
        row["tools_called"] = [c.name for c in getattr(result, "tool_calls", []) if getattr(c, "name", None)]
        row["skills_used"] = list(getattr(result, "skills_used", []) or [])
        checks, secret_hits = _run_checks(spec, workspace, reply)
        row["checks"] = checks
        row["secret_hits"] = secret_hits
        row["checks_total"] = len(checks)
        row["checks_passed"] = sum(1 for c in checks if c["ok"])
        row["score"] = round(100.0 * row["checks_passed"] / row["checks_total"], 1) if checks else None
        if mode == "mock":
            row["verdict"] = "MOCKED"  # never PASS: the pipeline was exercised, not the quality
            row["status_detail"] = "mock provider run — deterministic checks only"
        else:
            row["verdict"] = "PASS" if not secret_hits and row["checks_passed"] == row["checks_total"] else "FAIL"
            row["status_detail"] = "live run — deterministic checks only (LLM judge arrives in P5)"
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
        row["ts"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
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
    return {
        "version": REPORT_VERSION,
        "mode": mode,
        "repo_version": repo_version(),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "summary": {
            "tasks": len(rows),
            "ran": sum(1 for r in rows if r["verdict"] in {"MOCKED", "PASS", "FAIL"}),
            "skipped": sum(1 for r in rows if r["verdict"] == "SKIPPED"),
            "error": sum(1 for r in rows if r["verdict"] == "ERROR"),
            "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
            "secret_hits": sum(len(r.get("secret_hits") or []) for r in rows),
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
    lines.append(
        f"**Repo:** `{report['repo_version']}` · **Tasks:** `{summary['tasks']}` · "
        f"**Generated:** `{report['generated_at']}`"
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
