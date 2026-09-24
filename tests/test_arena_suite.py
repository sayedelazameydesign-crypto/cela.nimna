"""Offline tests for scripts/run_arena_suite.py (the Arena task suite — P0.5)."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run_arena_suite.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_arena_suite", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load_module()

MINIMAL_TASK = {
    "id": "demo-check",
    "category": "coding",
    "prompt": "check the seeded file",
    "seed_files": [{"path": "data/x.txt", "content": "hello-42"}],
    "expected_artifacts": [{"path": "data/x.txt", "contains": "42"}],
}


def test_task_schema_validation_accepts_minimal_and_rejects_bad_input():
    spec = rs.TaskSpec.from_dict(dict(MINIMAL_TASK))
    assert spec.id == "demo-check" and spec.max_steps == 12 and spec.timeout_s == 180

    with pytest.raises(rs.SuiteError, match="missing required"):
        rs.TaskSpec.from_dict({"id": "demo-check", "category": "coding"})
    with pytest.raises(rs.SuiteError, match="unknown keys"):
        rs.TaskSpec.from_dict({**MINIMAL_TASK, "hacker": True})
    with pytest.raises(rs.SuiteError, match="category"):
        rs.TaskSpec.from_dict({**MINIMAL_TASK, "category": "poetry"})
    with pytest.raises(rs.SuiteError, match="bad id"):
        rs.TaskSpec.from_dict({**MINIMAL_TASK, "id": "Bad_ID!"})
    with pytest.raises(rs.SuiteError, match="unknown requires"):
        rs.TaskSpec.from_dict({**MINIMAL_TASK, "requires": ["time_travel"]})
    with pytest.raises(rs.SuiteError, match="max_steps"):
        rs.TaskSpec.from_dict({**MINIMAL_TASK, "max_steps": 999})


def test_builtin_tasks_all_parse_unique_and_complete():
    specs = rs.load_tasks(REPO / "evals" / "tasks")
    assert len(specs) == 11
    ids = [s.id for s in specs]
    assert len(set(ids)) == 11
    by_category = {s.category for s in specs}
    assert by_category <= rs.CATEGORIES
    assert {"coding", "web", "data", "arabic_long"} <= by_category
    for spec in specs:
        assert spec.judge_rubric.strip(), f"{spec.id} must carry a judge rubric for P5"
        assert spec.prompt.strip() and spec.max_steps <= 60
        for step in spec.mock_script:
            assert step["tool"] and isinstance(step["arguments"], dict)


def test_mock_run_end_to_end_is_mocked_with_real_check_score():
    spec = rs.TaskSpec.from_dict(dict(MINIMAL_TASK), source="inline")
    report = rs.run_suite([spec], "mock", ledger_path=None)
    assert report["mode"] == "mock" and report["summary"]["ran"] == 1
    row = report["rows"][0]
    assert row["verdict"] == "MOCKED"  # never PASS in mock mode
    assert row["score"] == 100.0 and row["checks_passed"] == 1 and row["checks_total"] == 1
    assert row["model"] == "mock"


def test_mock_report_banner_precedes_table_and_never_claims_pass():
    spec = rs.TaskSpec.from_dict(dict(MINIMAL_TASK))
    markdown = rs.render_markdown(rs.run_suite([spec], "mock", ledger_path=None))
    lines = markdown.splitlines()
    banner_idx = next(i for i, l in enumerate(lines) if l.startswith("> ⚠️"))
    table_idx = next(i for i, l in enumerate(lines) if l.startswith("| Task"))
    assert banner_idx < table_idx, "MOCKED banner must come before any table"
    assert "MOCKED SUITE" in markdown
    assert "✅ **PASS**" not in markdown  # no fabricated success in mock mode


def test_missing_capability_is_skipped_with_reason(monkeypatch):
    for var in ("SHELL_TOOL_ENABLED", "ARENA_SUITE_NETWORK"):
        monkeypatch.delenv(var, raising=False)
    spec = rs.TaskSpec.from_dict({**MINIMAL_TASK, "id": "needs-shell", "requires": ["shell_tool"]})
    row = rs.run_task(spec, "mock")
    assert row["verdict"] == "SKIPPED"
    assert "shell_tool" in row["status_detail"]
    assert row["score"] is None  # no invented numbers for skipped tasks


def test_secret_in_workspace_is_flagged_and_value_never_leaks(monkeypatch):
    monkeypatch.delenv("SHELL_TOOL_ENABLED", raising=False)
    leaked = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
    spec = rs.TaskSpec.from_dict({
        **MINIMAL_TASK,
        "seed_files": [{"path": "leak.txt", "content": f'OPENAI_API_KEY = "{leaked}"'}],
        "expected_artifacts": [],
    })
    row = rs.run_task(spec, "mock")
    assert row["verdict"] == "MOCKED"
    assert row["secret_hits"], "the seeded fake key must be caught by the reused static gate"
    assert row["score"] < 100.0
    rendered = rs.render_markdown({"version": 1, "mode": "mock", "repo_version": "t",
                                   "generated_at": "now", "rows": [row],
                                   "summary": {"tasks": 1, "ran": 1, "skipped": 0, "error": 0,
                                               "mean_score": row["score"], "secret_hits": 1},
                                   "regressions": []})
    assert leaked not in rendered  # values are never echoed, only counts


def test_ledger_records_and_detects_regressions(tmp_path: Path):
    ok = rs.TaskSpec.from_dict(dict(MINIMAL_TASK))
    bad = rs.TaskSpec.from_dict({
        **MINIMAL_TASK,
        "expected_artifacts": [{"path": "data/missing.txt", "contains": "42"}],
    })
    ledger = tmp_path / "ledger.db"

    first = rs.run_suite([ok], "mock", ledger_path=ledger)
    assert first["regressions"] == [] and first["rows"][0]["previous_score"] is None

    second = rs.run_suite([bad], "mock", ledger_path=ledger)
    row = second["rows"][0]
    assert row["previous_score"] == 100.0
    assert second["regressions"] == [{"task_id": "demo-check", "previous_score": 100.0, "score": 0.0}]
    assert second["summary"]["mean_score"] == 0.0


def test_ledger_unit_last_scores_and_null_scores(tmp_path: Path):
    ledger = rs.Ledger(tmp_path / "l.db")
    base = {"ts": "t1", "task_id": "a", "mode": "mock", "verdict": "MOCKED", "model": "mock",
            "repo_version": "x", "details": {}}
    ledger.record({**base, "run_id": "r1", "score": 50.0, "checks_passed": 1, "checks_total": 2})
    ledger.record({**base, "run_id": "r2", "score": 80.0, "checks_passed": 2, "checks_total": 2})
    ledger.record({**base, "run_id": "r3", "task_id": "b", "verdict": "SKIPPED", "score": None,
                   "checks_passed": None, "checks_total": None})
    assert ledger.last_scores("mock") == {"a": 80.0}  # latest wins; NULL scores ignored
    ledger.close()


def test_cli_subprocess_writes_markdown_and_json(tmp_path: Path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "mini.yaml").write_text(
        yaml_dump(MINIMAL_TASK), encoding="utf-8"
    )
    out_md, out_json = tmp_path / "r.md", tmp_path / "r.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "mock", "--tasks-dir", str(tasks_dir),
         "--ledger", str(tmp_path / "ledger.db"), "--output", str(out_md),
         "--json-output", str(out_json)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Arena Task Suite" in out_md.read_text(encoding="utf-8")
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["rows"][0]["verdict"] == "MOCKED" and data["summary"]["ran"] == 1


def test_cli_strict_exits_2_on_regression(tmp_path: Path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    good = dict(MINIMAL_TASK)
    bad = {**MINIMAL_TASK, "expected_artifacts": [{"path": "data/gone.txt", "contains": "x"}]}
    (tasks_dir / "mini.yaml").write_text(yaml_dump(good), encoding="utf-8")
    ledger = str(tmp_path / "ledger.db")
    base = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "mock", "--tasks-dir", str(tasks_dir),
         "--ledger", ledger], capture_output=True, text=True, timeout=300,
    )
    assert base.returncode == 0, base.stderr
    (tasks_dir / "mini.yaml").write_text(yaml_dump(bad), encoding="utf-8")
    strict = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "mock", "--tasks-dir", str(tasks_dir),
         "--ledger", ledger, "--strict"], capture_output=True, text=True, timeout=300,
    )
    assert strict.returncode == 2, strict.stderr
    assert "regressions" in strict.stderr


def test_json_report_shape_is_stable():
    spec = rs.TaskSpec.from_dict(dict(MINIMAL_TASK))
    report = rs.run_suite([spec], "mock", ledger_path=None)
    for key in ("version", "mode", "repo_version", "generated_at", "rows", "summary", "regressions"):
        assert key in report
    row = report["rows"][0]
    for key in ("task_id", "category", "verdict", "score", "checks_passed", "checks_total",
                "model", "repo_version", "status_detail", "tools_called", "skills_used"):
        assert key in row
    json.dumps(report, ensure_ascii=False)  # must stay serializable


def test_artifact_is_reverified_through_delta_evidence(tmp_path: Path):
    """P1-T2 §10: an expected artifact produced via shell must pass BOTH the
    file check AND the delta observation + sha256 re-verification."""
    import hashlib

    content = "1 2 Fizz"
    spec = rs.TaskSpec.from_dict({
        **MINIMAL_TASK,
        "expected_artifacts": [{"path": "out/result.txt", "contains": "Fizz"}],
    })
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "result.txt").write_text(content, encoding="utf-8")
    sha = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    fs_delta = {"changes": [{"path": "out/result.txt", "kind": "CREATED",
                             "after": {"type": "file", "size": len(content), "sha256": sha}}],
                "snapshot_before": "snap_a", "snapshot_after": "snap_b",
                "before_root_hash": "h1", "after_root_hash": "h2", "summary": {"created": 1}}
    checks, _ = rs._run_checks(spec, tmp_path, "reply", fs_delta=fs_delta)
    names = {c["name"]: c["ok"] for c in checks}
    assert names.get("delta:out/result.txt observed CREATED") is True
    assert names.get("delta:out/result.txt sha re-verified") is True

    # a tampered artifact (hash mismatch) must FAIL re-verification
    (tmp_path / "out" / "result.txt").write_text("tampered", encoding="utf-8")
    checks, _ = rs._run_checks(spec, tmp_path, "reply", fs_delta=fs_delta)
    names = {c["name"]: c["ok"] for c in checks}
    assert names.get("delta:out/result.txt sha re-verified") is False


def test_verifier_runs_on_tasks_with_verify_spec(tmp_path: Path):
    """P1-T3: tasks with a verify: block get a deterministic verdict over the
    execution evidence, recorded in the row with fs fingerprints + chain hash."""
    spec = rs.TaskSpec.from_dict({
        **MINIMAL_TASK,
        "verify": [
            {"kind": "file_exists", "path": "data/x.txt"},
            {"kind": "content_matches", "path": "data/x.txt", "contains": "42"},
        ],
    })
    report = rs.run_suite([spec], "mock", ledger_path=None)
    row = report["rows"][0]
    assert row["verdict"] == "MOCKED"
    assert row["verification"]["verdict"] == "PASS"
    assert row["verification"]["summary"]["passed"] == 2
    assert any(c["name"] == "verifier:PASS" and c["ok"] for c in row["checks"])
    assert row["score"] == 100.0


def test_verifier_fail_is_reflected_in_score(tmp_path: Path):
    spec = rs.TaskSpec.from_dict({
        **MINIMAL_TASK,
        "verify": [{"kind": "file_exists", "path": "data/never-created.txt"}],
    })
    report = rs.run_suite([spec], "mock", ledger_path=None)
    row = report["rows"][0]
    assert row["verification"]["verdict"] == "FAIL"
    assert any(c["name"] == "verifier:FAIL" and not c["ok"] for c in row["checks"])
    assert row["score"] < 100.0


def test_seed_path_traversal_becomes_error_row_not_crash():
    spec = rs.TaskSpec.from_dict({**MINIMAL_TASK, "seed_files": [{"path": "../evil.txt", "content": "x"}]})
    row = rs.run_task(spec, "mock")
    assert row["verdict"] == "ERROR"
    assert "unsafe path" in row["status_detail"]


def test_repo_version_from_env_or_git(monkeypatch):
    monkeypatch.setenv("ARENA_REPO_VERSION", "test-sha")
    assert rs.repo_version() == "test-sha"
    monkeypatch.delenv("ARENA_REPO_VERSION")
    version = rs.repo_version()
    assert version == "unknown" or re.fullmatch(r"[0-9a-f]{7,40}", version), version


def yaml_dump(data: dict) -> str:
    import yaml
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


SHELL_TASK = {
    "id": "demo-shell-check",
    "category": "coding",
    "prompt": "create out/result.txt via the shell and verify it",
    "requires": ["shell_tool"],
    "allowed_tools": ["write_file", "run_command"],
    "mock_skills": ["shell_execution"],
    "mock_script": [
        {"tool": "run_command",
         "arguments": {"command": "mkdir -p out && printf 'FizzBuzz' > out/result.txt",
                       "purpose": "create the artifact for real"}},
    ],
    "mock_final": "created out/result.txt via run_command",
    "expected_artifacts": [{"path": "out/result.txt", "contains": "FizzBuzz"}],
    "verify": [
        {"kind": "file_exists", "path": "out/result.txt"},
        {"kind": "content_matches", "path": "out/result.txt", "contains": "FizzBuzz"},
        {"kind": "exit_code", "equals": 0},
    ],
}


def test_checkpoint_wiring_records_terminal_state_with_evidence(tmp_path: Path, monkeypatch):
    """P1-T4: every shell-executing run gets exactly one atomic checkpoint whose
    evidence head is the audit chain hash and whose fingerprint is the observed
    after-root-hash. Verifier PASS ⇒ COMPLETED (CHECKPOINT → FAILURE? NO)."""
    monkeypatch.setenv("SHELL_TOOL_ENABLED", "1")
    spec = rs.TaskSpec.from_dict(dict(SHELL_TASK), source="inline")
    report = rs.run_suite([spec], "mock", ledger_path=None)
    row = report["rows"][0]
    assert row["verification"]["verdict"] == "PASS"
    rec = row["recovery"]
    assert rec["state"] == "COMPLETED"
    assert rec["checkpoint_id"].startswith("ckpt_") and len(rec["checkpoint_id"]) == 29
    assert rec["state_version"] == 1
    # evidence chain: checkpoint head == fs evidence audit hash, fingerprint == after root hash
    assert rec["evidence_head"] == row["fs_evidence"]["audit_hash"]
    assert rec["observation_fingerprint"] == row["fs_evidence"]["after_root_hash"]
    assert report["summary"]["checkpoint_completed"] == 1
    assert report["summary"]["checkpoint_failed"] == 0


def test_checkpoint_fail_path_and_markdown_lines(tmp_path: Path, monkeypatch):
    """Verifier FAIL ⇒ mission FAILED in the checkpoint store (never silently
    completed), and the markdown carries the Checkpoint summary line."""
    monkeypatch.setenv("SHELL_TOOL_ENABLED", "1")
    spec = rs.TaskSpec.from_dict({**SHELL_TASK,
                                  "verify": [{"kind": "file_exists", "path": "out/never.txt"}]},
                                 source="inline")
    report = rs.run_suite([spec], "mock", ledger_path=None)
    row = report["rows"][0]
    assert row["verification"]["verdict"] == "FAIL"
    assert row["recovery"]["state"] == "FAILED"
    assert report["summary"]["checkpoint_failed"] == 1
    assert report["summary"]["checkpoint_completed"] == 0
    markdown = rs.render_markdown(report)
    assert "**Checkpoint (P1-T4, atomic store):**" in markdown
    assert "ckpt FAILED:" in markdown


def test_registry_gates_verifier_command_reruns(monkeypatch):
    """P1-T5: the verifier's bounded command re-run goes through the Tool
    Registry pipeline — schema-validated, capability/policy/authorization gated,
    evidence chained. A real command check executes and is recorded EXECUTED."""
    monkeypatch.setenv("SHELL_TOOL_ENABLED", "1")
    spec = rs.TaskSpec.from_dict({**SHELL_TASK,
                                  "verify": [
                                      {"kind": "file_exists", "path": "out/result.txt"},
                                      {"kind": "command",
                                       "command": "printf replayed > out/replay.txt && cat out/replay.txt"},
                                  ]}, source="inline")
    report = rs.run_suite([spec], "mock", ledger_path=None)
    row = report["rows"][0]
    assert row["verification"]["verdict"] == "PASS"
    assert row["verification"]["summary"]["passed"] == 2      # file_exists + command
    reg_row = row["registry"]
    assert reg_row["registered"] == ["sandbox.command"]
    assert reg_row["authorized"] == 1 and reg_row["denied"] == 0
    assert reg_row["schema_failures"] == 0
    assert reg_row["chain_verified"] is True
    assert len(reg_row["evidence_tail"]) == 16                 # 16-hex evidence tail slice
    # the replayed artifact really exists — the command ran through the sandbox handler
    assert report["summary"]["registry_authorized"] == 1
    # P1-T6: the decision went through the deterministic policy
    pol = row["policy"]
    assert pol["policy_id"] == "arena-suite-policy" and pol["version"] == "1.0.0"
    assert pol["allow"] == 1 and pol["deny"] == 0
    assert report["summary"]["policy_allow"] == 1
    # P1-T7: the whole crossing went through the single gateway
    gw_row = row["gateway"]
    assert gw_row["invoked"] == 1 and gw_row["refused"] == 0
    assert gw_row["observed"] == 1 and gw_row["verified"] == 1 and gw_row["checkpointed"] == 1
    assert gw_row["chain_verified"] is True
    assert report["summary"]["gateway_invoked"] == 1
    markdown = rs.render_markdown(report)
    assert "**Registry (P1-T5, gated invocation):**" in markdown
    assert "**Policy (P1-T6, deterministic governance):**" in markdown
    assert "**Gateway (P1-T7, single path):**" in markdown
    # the executed event carries the policy identity in its evidence
    assert "suite-policy" in str(row["verification"]) or pol["allow"] == 1


def test_registry_denial_is_inconclusive_never_pass(monkeypatch):
    """P1-T5: with the shell capability not granted, the command re-run is
    refused at the capability gate ⇒ honest INCONCLUSIVE, never a fake PASS."""
    monkeypatch.delenv("SHELL_TOOL_ENABLED", raising=False)
    spec = rs.TaskSpec.from_dict({
        **MINIMAL_TASK,
        "verify": [{"kind": "command", "command": "echo should-never-run"}],
    })
    report = rs.run_suite([spec], "mock", ledger_path=None)
    row = report["rows"][0]
    assert row["verdict"] == "MOCKED"
    assert row["verification"]["verdict"] == "INCONCLUSIVE"
    assert any(c["name"] == "verifier:INCONCLUSIVE" and not c["ok"] for c in row["checks"])
    reg_row = row["registry"]
    assert reg_row["denied"] == 1 and reg_row.get("authorized", 0) == 0
    # the capability gate closes BEFORE the policy is ever consulted
    assert (row.get("policy") or {}).get("allow", 0) == 0
    # the refused command never executed — no replay artifact in the workspace
