"""Deterministic Verification — the third Execution Fabric primitive (P1-T3).

Contract (owner): the Verifier is **not an LLM**. It consumes evidence
(shell exit codes, the filesystem delta from P1-T2, and the workspace itself)
plus a declarative spec, and returns one of exactly three verdicts:

    PASS   — every check evaluated and passed
    FAIL   — at least one check evaluated and failed
    INCONCLUSIVE — zero checks, or checks that could not be evaluated
                   (missing evidence, unreadable file, denied command)

…never anything else, and never a fabricated PASS. The LLM Judge comes
*after* this primitive (Deterministic Verifier → Evidence → LLM Judge).

Check vocabulary (declarative, YAML-friendly dicts):

    file_exists      {path}
    file_absent      {path}
    content_matches  {path, contains?|equals?|regex?|sha256?}   (all given must hold)
    exit_code        {equals?=0}          → against the shell execution evidence
    delta            {path, change?=CREATED}  → observed in the P1-T2 delta
    delta_sha        {path}               → re-verified by re-observation (hash)
    json_keys        {path, required=[…]} → valid JSON + required keys
    command          {command, timeout_ms?=30000} → bounded verification re-run
                                                     via the shell primitive;
                                                     DENIED/CONFIRMATION_REQUIRED
                                                     ⇒ INCONCLUSIVE (not FAIL)

Security: every path is contained by resolve → containment against the
workspace root (lexical first so symlink entries stay checkable; a resolved
escape is refused). The verifier reads files and re-observes; it never
writes. ``command`` checks execute only through the caller-provided executor
(the suite passes the P1-T1 sandboxed shell with explicit consent).

Honesty: an empty spec is INCONCLUSIVE, not PASS. A check that cannot be
evaluated is INCONCLUSIVE, not skipped-silently. ``PASS`` requires positive
evidence for every declared check.
"""
from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .observation import FilesystemDelta, WorkspaceObserver

VERIFIER_VERSION = 1

CHECK_KINDS = {
    "file_exists", "file_absent", "content_matches", "exit_code",
    "delta", "delta_sha", "json_keys", "command",
}
PATH_KINDS = {"file_exists", "file_absent", "content_matches", "delta", "delta_sha", "json_keys"}
CHANGE_KINDS = {"CREATED", "MODIFIED", "DELETED", "RENAMED"}
MAX_SPEC_CHECKS = 32
MAX_VERIFY_COMMAND_CHARS = 2_000
INCONCLUSIVE = None  # check outcome encoding: True pass, False fail, None inconclusive


class Verdict(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class SpecError(ValueError):
    """The verification spec itself is malformed — caller error, honest abort."""


@dataclass
class CheckResult:
    kind: str
    ok: bool | None          # True / False / None (inconclusive)
    detail: str
    target: str = ""

    @property
    def inconclusive(self) -> bool:
        return self.ok is None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ok": self.ok, "detail": self.detail, "target": self.target}


@dataclass
class VerificationReport:
    verdict: Verdict
    checks: list[CheckResult] = field(default_factory=list)
    version: int = VERIFIER_VERSION

    @property
    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c.ok is True),
            "failed": sum(1 for c in self.checks if c.ok is False),
            "inconclusive": sum(1 for c in self.checks if c.ok is None),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


def validate_spec(spec: Any) -> list[dict[str, Any]]:
    """Validate a declarative verification spec; raise :class:`SpecError` honestly."""
    if spec is None:
        return []
    if not isinstance(spec, list) or len(spec) > MAX_SPEC_CHECKS:
        raise SpecError(f"verify spec must be a list of at most {MAX_SPEC_CHECKS} checks")
    validated: list[dict[str, Any]] = []
    for index, entry in enumerate(spec):
        if not isinstance(entry, dict) or not str(entry.get("kind") or "").strip():
            raise SpecError(f"verify[{index}]: every check needs a 'kind'")
        kind = str(entry["kind"])
        if kind not in CHECK_KINDS:
            raise SpecError(f"verify[{index}]: unknown check kind {kind!r} (known: {sorted(CHECK_KINDS)})")
        if kind in PATH_KINDS and not str(entry.get("path") or "").strip():
            raise SpecError(f"verify[{index}]: {kind} needs a 'path'")
        if kind == "command" and not str(entry.get("command") or "").strip():
            raise SpecError(f"verify[{index}]: command check needs a 'command'")
        if kind == "delta":
            change = str(entry.get("change") or "CREATED").upper()
            if change not in CHANGE_KINDS:
                raise SpecError(f"verify[{index}]: delta change must be one of {sorted(CHANGE_KINDS)}")
        validated.append(entry)
    return validated


class DeterministicVerifier:
    """Evidence-consuming, LLM-free verifier over one workspace jail."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()
        self.observer = WorkspaceObserver(workspace_root)

    # ------------------------------------------------------------------ #
    def verify(
        self,
        spec: list[dict[str, Any]] | None,
        *,
        shell_exit_code: int | None = None,
        shell_timed_out: bool = False,
        fs_delta: dict[str, Any] | None = None,
        exec_fn: Callable[[str, int], Any] | None = None,
    ) -> VerificationReport:
        """Run the spec. ``shell_exit_code``/``fs_delta`` carry the execution
        evidence; ``exec_fn(command, timeout_ms)`` runs bounded verification
        commands (the suite wires this to the P1-T1 sandboxed shell)."""
        try:
            checks_spec = validate_spec(spec)
        except SpecError as exc:
            return VerificationReport(Verdict.INCONCLUSIVE, [
                CheckResult("spec", None, f"invalid verification spec: {exc}")
            ])

        results: list[CheckResult] = []
        for entry in checks_spec:
            kind = str(entry["kind"])
            try:
                if kind == "file_exists":
                    results.append(self._file_exists(entry))
                elif kind == "file_absent":
                    results.append(self._file_absent(entry))
                elif kind == "content_matches":
                    results.append(self._content_matches(entry))
                elif kind == "exit_code":
                    results.append(self._exit_code(entry, shell_exit_code, shell_timed_out))
                elif kind == "delta":
                    results.append(self._delta(entry, fs_delta))
                elif kind == "delta_sha":
                    results.append(self._delta_sha(entry, fs_delta))
                elif kind == "json_keys":
                    results.append(self._json_keys(entry))
                elif kind == "command":
                    results.append(self._command(entry, exec_fn))
                else:  # pragma: no cover - validate_spec already filters
                    results.append(CheckResult(kind, None, "unhandled check kind"))
            except Exception as exc:  # a crashing check is inconclusive, never a PASS
                results.append(CheckResult(kind, None,
                                           f"check crashed: {exc.__class__.__name__}: {str(exc)[:120]}",
                                           target=str(entry.get("path") or entry.get("command") or "")))

        if not results:
            return VerificationReport(Verdict.INCONCLUSIVE, [
                CheckResult("spec", None, "empty verification spec — nothing verified, never PASS")
            ])
        if any(r.ok is False for r in results):
            verdict = Verdict.FAIL
        elif any(r.inconclusive for r in results):
            verdict = Verdict.INCONCLUSIVE
        else:
            verdict = Verdict.PASS
        return VerificationReport(verdict, results)

    # ------------------------------------------------------------------ #
    def _resolve(self, raw: str) -> tuple[Path | None, str]:
        """Contain a spec path inside the workspace (lexical then resolved)."""
        rel = str(raw).strip()
        if not rel or "\x00" in rel:
            return None, "empty or invalid path"
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None, f"path escapes the workspace: {raw!r}"
        target = self.workspace_root / candidate
        try:
            resolved = target.resolve()
            resolved.relative_to(self.workspace_root)
        except (ValueError, OSError) as exc:
            return None, f"path escape on resolve: {exc.__class__.__name__}"
        return target, ""

    def _file_exists(self, entry: dict[str, Any]) -> CheckResult:
        target, escape = self._resolve(entry["path"])
        if escape:
            return CheckResult("file_exists", False, escape, target=str(entry["path"]))
        if target.is_file():
            return CheckResult("file_exists", True, "file present", target=str(entry["path"]))
        if target.is_dir():
            return CheckResult("file_exists", False, "path exists but is a directory", target=str(entry["path"]))
        return CheckResult("file_exists", False, "file missing", target=str(entry["path"]))

    def _file_absent(self, entry: dict[str, Any]) -> CheckResult:
        target, escape = self._resolve(entry["path"])
        if escape:
            return CheckResult("file_absent", False, escape, target=str(entry["path"]))
        if target.exists():
            return CheckResult("file_absent", False, "file present but expected absent", target=str(entry["path"]))
        return CheckResult("file_absent", True, "absent as required", target=str(entry["path"]))

    def _content_matches(self, entry: dict[str, Any]) -> CheckResult:
        path_label = str(entry["path"])
        target, escape = self._resolve(entry["path"])
        if escape:
            return CheckResult("content_matches", False, escape, target=path_label)
        if not target.is_file():
            return CheckResult("content_matches", False, "file missing", target=path_label)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return CheckResult("content_matches", None, f"unreadable: {exc.__class__.__name__}", target=path_label)

        conditions: list[tuple[str, bool]] = []
        if "contains" in entry and entry["contains"]:
            needle = str(entry["contains"])
            conditions.append((f"contains {needle!r}", needle in text))
        if "equals" in entry and entry["equals"] is not None:
            expected = str(entry["equals"])
            conditions.append((f"equals {expected!r}", text.strip() == expected))
        if "regex" in entry and entry["regex"]:
            try:
                matched = re.search(str(entry["regex"]), text) is not None
            except re.error as exc:
                return CheckResult("content_matches", None, f"invalid regex: {exc}", target=path_label)
            conditions.append((f"regex {entry['regex']!r}", matched))
        if "sha256" in entry and entry["sha256"]:
            expected = str(entry["sha256"])
            expected_hex = expected.split(":", 1)[1] if ":" in expected else expected
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            conditions.append(("sha256", actual == expected_hex))
        if not conditions:
            return CheckResult("content_matches", None, "no condition given (contains/equals/regex/sha256)",
                               target=path_label)
        failed = [label for label, ok in conditions if not ok]
        detail = "; ".join(label for label, _ in conditions) + (" — all matched" if not failed else f" — failed: {'; '.join(failed)}")
        return CheckResult("content_matches", not failed, detail, target=path_label)

    def _exit_code(self, entry: dict[str, Any], shell_exit_code: int | None, timed_out: bool) -> CheckResult:
        expected = int(entry.get("equals", 0))
        if timed_out or shell_exit_code is None:
            return CheckResult("exit_code", None,
                               "no execution evidence to verify (no shell run or timed out)")
        ok = shell_exit_code == expected
        return CheckResult("exit_code", ok, f"exit {shell_exit_code} (expected {expected})")

    def _delta(self, entry: dict[str, Any], fs_delta: dict[str, Any] | None) -> CheckResult:
        path_label = str(entry["path"])
        if not fs_delta:
            return CheckResult("delta", None, "no filesystem delta evidence", target=path_label)
        change = str(entry.get("change") or "CREATED").upper()
        observed = [c for c in (fs_delta.get("changes") or []) if c.get("path") == path_label]
        if not observed:
            return CheckResult("delta", False, f"{path_label} not observed in the delta", target=path_label)
        ok = any(str(c.get("kind")) == change for c in observed)
        kinds = ", ".join(sorted({str(c.get("kind")) for c in observed}))
        return CheckResult("delta", ok,
                           (f"observed {kinds}" if ok else f"observed {kinds}, expected {change}"),
                           target=path_label)

    def _delta_sha(self, entry: dict[str, Any], fs_delta: dict[str, Any] | None) -> CheckResult:
        path_label = str(entry["path"])
        if not fs_delta:
            return CheckResult("delta_sha", None, "no filesystem delta evidence", target=path_label)
        delta = FilesystemDelta.from_payload(fs_delta)
        results = {r["path"]: r for r in self.observer.verify_delta(delta)}
        result = results.get(path_label)
        if result is None:
            return CheckResult("delta_sha", None, f"{path_label} is not part of the delta", target=path_label)
        return CheckResult("delta_sha", bool(result.get("match")),
                           result.get("note") or "re-observation result", target=path_label)

    def _json_keys(self, entry: dict[str, Any]) -> CheckResult:
        path_label = str(entry["path"])
        target, escape = self._resolve(entry["path"])
        if escape:
            return CheckResult("json_keys", False, escape, target=path_label)
        if not target.is_file():
            return CheckResult("json_keys", False, "file missing", target=path_label)
        try:
            data = json.loads(target.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            return CheckResult("json_keys", False, f"invalid JSON: {exc.__class__.__name__}", target=path_label)
        required = [str(k) for k in (entry.get("required") or [])]
        if not isinstance(data, dict):
            return CheckResult("json_keys", False, "JSON root is not an object", target=path_label)
        missing = [key for key in required if key not in data]
        if missing:
            return CheckResult("json_keys", False, f"missing keys: {missing}", target=path_label)
        return CheckResult("json_keys", True,
                           f"valid JSON object with required keys {required or '(none declared)'}",
                           target=path_label)

    def _command(self, entry: dict[str, Any], exec_fn: Callable[[str, int], Any] | None) -> CheckResult:
        command = str(entry["command"]).strip()
        if len(command) > MAX_VERIFY_COMMAND_CHARS:
            return CheckResult("command", None, "verification command too long", target=command[:80])
        if exec_fn is None:
            return CheckResult("command", None,
                               "no executor wired for command checks (capability disabled?)", target=command[:80])
        timeout_ms = int(entry.get("timeout_ms") or 30_000)
        result = exec_fn(command, timeout_ms)
        status = str(getattr(result, "status", "") or (result or {}).get("status") if not isinstance(result, dict) else result.get("status") or "")
        if status == "SUCCESS":
            return CheckResult("command", True, "verification command succeeded", target=command[:80])
        if status in {"NONZERO_EXIT", "TIMEOUT"}:
            return CheckResult("command", False, f"verification command {status}", target=command[:80])
        # DENIED / CONFIRMATION_REQUIRED / POLICY_BLOCKED / SANDBOX_ERROR / unknown
        return CheckResult("command", None, f"could not evaluate (status {status or 'unknown'})", target=command[:80])
