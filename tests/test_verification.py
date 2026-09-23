"""Tests for the Deterministic Verification primitive (P1-T3).

Contract: the Verifier is not an LLM — PASS / FAIL / INCONCLUSIVE only,
built on P1-T2 evidence (delta + re-observation) and shell exit evidence.
An empty or non-evaluable spec is INCONCLUSIVE, never a fabricated PASS.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nimna.execution.observation import WorkspaceObserver
from nimna.execution.verification import (
    DeterministicVerifier,
    SpecError,
    Verdict,
    validate_spec,
)

CONTENT = "1 2 Fizz 4 Buzz FizzBuzz"
CONTENT_SHA = "sha256:" + hashlib.sha256(CONTENT.encode()).hexdigest()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "result.txt").write_text(CONTENT, encoding="utf-8")
    (tmp_path / "out" / "data.json").write_text('{"orders": [1, 2], "total": 3}', encoding="utf-8")
    return tmp_path


def make_delta(workspace: Path) -> dict:
    """A real delta payload from an actual snapshot pair (not hand-made)."""
    empty_dir = workspace / ".."  # noqa: F841 - observer works on any root; use the workspace itself
    observer = WorkspaceObserver(workspace)
    # simulate: before-snapshot without the files, after-snapshot with them
    (workspace / "out" / "result.txt").unlink()
    before = observer.snapshot()
    (workspace / "out" / "result.txt").write_text(CONTENT, encoding="utf-8")
    after = observer.snapshot()
    delta = observer.delta(before, after)
    return delta.to_dict()


def verdict_of(verifier: DeterministicVerifier, spec, **kwargs) -> Verdict:
    return verifier.verify(spec, **kwargs).verdict


# --------------------------------------------------------------------------- #
# PASS — positive evidence for every check
# --------------------------------------------------------------------------- #
def test_full_spec_passes_with_real_evidence(workspace: Path):
    delta = make_delta(workspace)
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "file_exists", "path": "out/result.txt"},
        {"kind": "content_matches", "path": "out/result.txt", "contains": "FizzBuzz"},
        {"kind": "content_matches", "path": "out/result.txt", "sha256": CONTENT_SHA},
        {"kind": "exit_code", "equals": 0},
        {"kind": "delta", "path": "out/result.txt", "change": "CREATED"},
        {"kind": "delta_sha", "path": "out/result.txt"},
        {"kind": "json_keys", "path": "out/data.json", "required": ["orders", "total"]},
    ], shell_exit_code=0, fs_delta=delta)
    assert report.verdict is Verdict.PASS
    assert report.summary["passed"] == report.summary["total"] == 7
    payload = report.to_dict()
    assert payload["verdict"] == "PASS" and payload["summary"]["failed"] == 0


def test_content_matches_equals_and_regex(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "content_matches", "path": "out/result.txt", "equals": CONTENT},
        {"kind": "content_matches", "path": "out/result.txt", "regex": r"Fizz\s*\d*"},
    ])
    assert report.verdict is Verdict.PASS


# --------------------------------------------------------------------------- #
# FAIL — a real failing check, with a precise detail
# --------------------------------------------------------------------------- #
def test_fail_on_missing_file_and_absent_violation(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "file_exists", "path": "out/missing.txt"},
        {"kind": "file_absent", "path": "out/result.txt"},   # it exists → violation
    ])
    assert report.verdict is Verdict.FAIL
    details = {c.target: c.detail for c in report.checks}
    assert "missing" in details["out/missing.txt"]
    assert "expected absent" in details["out/result.txt"]


def test_fail_dominates_inconclusive_and_invalid_regex_is_inconclusive(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "content_matches", "path": "out/result.txt", "contains": "Python"},
        {"kind": "content_matches", "path": "out/result.txt", "regex": r"[unclosed"},
    ])
    # pass + inconclusive → INCONCLUSIVE; then a real failure dominates:
    report2 = verifier.verify([
        {"kind": "content_matches", "path": "out/result.txt", "contains": "FizzBuzz"},
        {"kind": "content_matches", "path": "out/result.txt", "regex": r"[unclosed"},
    ])
    assert report2.verdict is Verdict.INCONCLUSIVE and report2.summary["inconclusive"] == 1
    report3 = verifier.verify([
        {"kind": "content_matches", "path": "out/result.txt", "contains": "Python"},
        {"kind": "content_matches", "path": "out/result.txt", "regex": r"[unclosed"},
    ])
    assert report3.verdict is Verdict.FAIL
    assert report3.summary["failed"] == 1 and report3.summary["inconclusive"] == 1


def test_fail_on_wrong_sha_and_wrong_exit_code(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "content_matches", "path": "out/result.txt", "sha256": "sha256:" + "0" * 64},
        {"kind": "exit_code", "equals": 0},
    ], shell_exit_code=3)
    assert report.verdict is Verdict.FAIL
    assert report.summary["failed"] == 2


def test_fail_on_json_invalid_or_missing_keys(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    (workspace / "out" / "broken.json").write_text("{not json", encoding="utf-8")
    report = verifier.verify([
        {"kind": "json_keys", "path": "out/broken.json", "required": ["a"]},
        {"kind": "json_keys", "path": "out/data.json", "required": ["nope"]},
    ])
    assert report.verdict is Verdict.FAIL
    assert any("invalid JSON" in c.detail for c in report.checks)
    assert any("missing keys" in c.detail for c in report.checks)


def test_delta_checks_fail_when_change_not_observed(workspace: Path):
    delta = make_delta(workspace)
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "delta", "path": "out/absent.txt", "change": "CREATED"},
        {"kind": "delta", "path": "out/result.txt", "change": "DELETED"},  # actually CREATED
    ], fs_delta=delta)
    assert report.verdict is Verdict.FAIL
    assert any("not observed" in c.detail for c in report.checks)
    assert any("expected DELETED" in c.detail for c in report.checks)


def test_delta_sha_fails_after_tampering(workspace: Path):
    delta = make_delta(workspace)
    (workspace / "out" / "result.txt").write_text("tampered", encoding="utf-8")  # AFTER the delta
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([{"kind": "delta_sha", "path": "out/result.txt"}], fs_delta=delta)
    assert report.verdict is Verdict.FAIL
    assert "mismatch" in report.checks[0].detail


# --------------------------------------------------------------------------- #
# INCONCLUSIVE — missing evidence is never silently PASS
# --------------------------------------------------------------------------- #
def test_empty_spec_is_inconclusive_never_pass(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([])
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "never PASS" in report.checks[0].detail


def test_missing_evidence_is_inconclusive(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "exit_code", "equals": 0},
        {"kind": "delta", "path": "out/result.txt"},
        {"kind": "delta_sha", "path": "out/result.txt"},
    ])  # no shell evidence at all
    assert report.verdict is Verdict.INCONCLUSIVE
    assert report.summary["inconclusive"] == 3


def test_timed_out_shell_exit_is_inconclusive_not_fail(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([{"kind": "exit_code", "equals": 0}],
                             shell_exit_code=None, shell_timed_out=True)
    assert report.verdict is Verdict.INCONCLUSIVE


def test_command_check_without_executor_is_inconclusive(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([{"kind": "command", "command": "true"}], exec_fn=None)
    assert report.verdict is Verdict.INCONCLUSIVE


def test_command_check_maps_statuses_honestly(workspace: Path):
    class R:
        def __init__(self, status):
            self.status = status

    verifier = DeterministicVerifier(workspace)
    spec = [{"kind": "command", "command": "true"}]
    assert verifier.verify(spec, exec_fn=lambda c, t: R("SUCCESS")).verdict is Verdict.PASS
    assert verifier.verify(spec, exec_fn=lambda c, t: R("NONZERO_EXIT")).verdict is Verdict.FAIL
    assert verifier.verify(spec, exec_fn=lambda c, t: R("CONFIRMATION_REQUIRED")).verdict is Verdict.INCONCLUSIVE
    assert verifier.verify(spec, exec_fn=lambda c, t: R("DENIED")).verdict is Verdict.INCONCLUSIVE


# --------------------------------------------------------------------------- #
# security & spec validation
# --------------------------------------------------------------------------- #
def test_path_escapes_are_failures_not_crashes(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([
        {"kind": "file_exists", "path": "../../etc/passwd"},
        {"kind": "file_exists", "path": "/etc/passwd"},
        {"kind": "content_matches", "path": "out/../../../etc/passwd", "contains": "root"},
    ])
    assert report.verdict is Verdict.FAIL
    assert all("escapes the workspace" in c.detail for c in report.checks)


def test_symlinked_file_content_check_stays_inside_and_passes_or_inconcluses(workspace: Path):
    import os
    (workspace / "real.txt").write_text("inside", encoding="utf-8")
    os.symlink(workspace / "real.txt", workspace / "out" / "link.txt")
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([{"kind": "content_matches", "path": "out/link.txt", "contains": "inside"}])
    assert report.verdict is Verdict.PASS  # in-jail symlink is a legitimate read target


def test_validate_spec_rejects_malformed_specs():
    with pytest.raises(SpecError):
        validate_spec([{"path": "x"}])                       # no kind
    with pytest.raises(SpecError):
        validate_spec([{"kind": "mind_read", "path": "x"}])  # unknown kind
    with pytest.raises(SpecError):
        validate_spec([{"kind": "file_exists"}])             # no path
    with pytest.raises(SpecError):
        validate_spec([{"kind": "command"}])                 # no command
    with pytest.raises(SpecError):
        validate_spec([{"kind": "delta", "path": "x", "change": "EXPLODED"}])
    with pytest.raises(SpecError):
        validate_spec([{"kind": "file_exists", "path": "x"}] * 33)  # cap
    assert validate_spec(None) == []
    assert validate_spec([{"kind": "file_exists", "path": "x"}])


def test_invalid_spec_yields_inconclusive_not_crash(workspace: Path):
    verifier = DeterministicVerifier(workspace)
    report = verifier.verify([{"kind": "nope"}])
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "invalid verification spec" in report.checks[0].detail


def test_report_dict_is_stable_and_json_safe(workspace: Path):
    import json
    verifier = DeterministicVerifier(workspace)
    payload = verifier.verify([{"kind": "file_exists", "path": "out/result.txt"}]).to_dict()
    assert set(payload) == {"version", "verdict", "summary", "checks"}
    json.dumps(payload, ensure_ascii=False)
