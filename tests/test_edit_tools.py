"""T8 — File/Edit فوق Fabric: precise edit_file + all-or-nothing apply_patch.

Legacy door (pydantic tools) + Fabric door (descriptors through policy →
authorization → executor → hash-chained evidence). Offline, deterministic.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from nimna.core.state import RunStatus
from nimna.providers.base import ModelResponse, ToolCall
from nimna.tools.base import ToolContext
from nimna.tools.builtin import edit as edit_mod
from nimna.tools.builtin.edit import EDIT_MAX_BYTES, apply_edit
from test_binding_migration import make_gateway


# ---------------------------------------------------------------- helpers ---
@pytest.fixture
def exec_ctx(workspace, settings):
    return ToolContext(settings=settings, workspace=workspace, session_id="t8")


def _run(registry, name, arguments, ctx):
    res, ok, _ = registry.execute(name, arguments, ctx)
    return json.loads(res), ok


def _make_resolve(ws):
    def resolve(raw: str) -> Path:
        p = (ws / raw).resolve()
        if not p.is_relative_to(ws.resolve()):
            raise edit_mod.ToolError(f"path escapes the workspace jail: {raw!r}")
        return p
    return resolve


def _fabric_registry(workspace):
    from nimna.execution.tool_registry import ToolRegistry
    from nimna.tools.builtin.edit import register_fabric
    reg = ToolRegistry()
    register_fabric(reg, workspace_root=workspace, resolve=_make_resolve(workspace))
    return reg


# ------------------------------------------------------- legacy: edit_file ---
def test_edit_replaces_single_occurrence(workspace, exec_ctx):
    from nimna.tools import default_registry
    reg = default_registry()
    out, ok = _run(reg, "edit_file",
                   {"path": "notes.txt", "old_text": "hello", "new_text": "salam"}, exec_ctx)
    assert ok and out["status"] == "EDITED" and out["replaced"] == 1
    assert (workspace / "notes.txt").read_text() == "salam\nworld\n"


def test_edit_missing_old_text_fails_and_writes_nothing(workspace, exec_ctx):
    from nimna.tools import default_registry
    before = (workspace / "notes.txt").read_text()
    out, ok = _run(default_registry(), "edit_file",
                   {"path": "notes.txt", "old_text": "absent-text", "new_text": "x"}, exec_ctx)
    assert not ok and "not found" in out["error"]
    assert (workspace / "notes.txt").read_text() == before


def test_edit_ambiguous_match_is_refused_unless_expect_once_false(workspace, exec_ctx):
    (workspace / "dup.txt").write_text("aa\n", encoding="utf-8")
    from nimna.tools import default_registry
    reg = default_registry()
    out, ok = _run(reg, "edit_file",
                   {"path": "dup.txt", "old_text": "a", "new_text": "b"}, exec_ctx)
    assert not ok and "ambiguous" in out["error"]          # expect_once default
    assert (workspace / "dup.txt").read_text() == "aa\n"
    out, ok = _run(reg, "edit_file",
                   {"path": "dup.txt", "old_text": "a", "new_text": "b",
                    "expect_once": False}, exec_ctx)
    assert ok and out["replaced"] == 2
    assert (workspace / "dup.txt").read_text() == "bb\n"


def test_edit_identical_texts_refused(workspace, exec_ctx):
    from nimna.tools import default_registry
    out, ok = _run(default_registry(), "edit_file",
                   {"path": "notes.txt", "old_text": "same", "new_text": "same"}, exec_ctx)
    assert not ok and "identical" in out["error"]


def test_edit_jail_escape_refused_and_nothing_written_outside(workspace, exec_ctx):
    from nimna.tools import default_registry
    out, ok = _run(default_registry(), "edit_file",
                   {"path": "../evil.txt", "old_text": "x", "new_text": "y"}, exec_ctx)
    assert not ok and "jail" in out["error"]
    assert not (workspace.parent / "evil.txt").exists()


def test_edit_oversize_and_binary_refused(workspace, exec_ctx):
    from nimna.tools import default_registry
    reg = default_registry()
    (workspace / "big.txt").write_text("x" * (EDIT_MAX_BYTES + 1), encoding="utf-8")
    out, ok = _run(reg, "edit_file",
                   {"path": "big.txt", "old_text": "x", "new_text": "y"}, exec_ctx)
    assert not ok and "exceeds" in out["error"]
    (workspace / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
    out, ok = _run(reg, "edit_file",
                   {"path": "bin.dat", "old_text": "x", "new_text": "y"}, exec_ctx)
    assert not ok and "binary" in out["error"]


# --------------------------------------------------- legacy: apply_patch ----
PATCH = """--- a/code.txt
+++ b/code.txt
@@ -1,3 +1,3 @@
 line1
-line2
+line-two
 line3
@@ -5,2 +5,3 @@
 line5
+inserted
 line6
"""


def _seed_code(workspace):
    (workspace / "code.txt").write_text(
        "line1\nline2\nline3\nline4\nline5\nline6\n", encoding="utf-8")


def test_apply_patch_two_hunks_all_or_nothing(workspace, exec_ctx):
    _seed_code(workspace)
    from nimna.tools import default_registry
    out, ok = _run(default_registry(), "apply_patch", {"diff": PATCH}, exec_ctx)
    assert ok and out["status"] == "PATCHED" and out["files"] == ["code.txt"] and out["hunks"] == 2
    assert (workspace / "code.txt").read_text() == \
        "line1\nline-two\nline3\nline4\nline5\ninserted\nline6\n"


def test_apply_patch_context_mismatch_writes_nothing(workspace, exec_ctx):
    _seed_code(workspace)
    bad = PATCH.replace(" line3", " WRONG-CONTEXT")
    from nimna.tools import default_registry
    out, ok = _run(default_registry(), "apply_patch", {"diff": bad}, exec_ctx)
    assert not ok and "context mismatch" in out["error"]
    assert (workspace / "code.txt").read_text().startswith("line1\nline2\n")   # untouched


def test_apply_patch_malformed_lines_are_explicit(workspace, exec_ctx):
    from nimna.tools import default_registry
    out, ok = _run(default_registry(), "apply_patch",
                   {"diff": "this is not a diff\n"}, exec_ctx)
    assert not ok and "malformed patch at line 1" in out["error"]
    out, ok = _run(default_registry(), "apply_patch",
                   {"diff": "--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n\\ No newline at end of file\n"},
                   exec_ctx)
    assert not ok and "not supported" in out["error"]


def test_apply_patch_devnull_and_rename_refused(workspace, exec_ctx):
    from nimna.tools import default_registry
    reg = default_registry()
    out, ok = _run(reg, "apply_patch",
                   {"diff": "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+x\n"}, exec_ctx)
    assert not ok and "/dev/null" in out["error"]
    out, ok = _run(reg, "apply_patch",
                   {"diff": "--- a/x.txt\n+++ b/y.txt\n@@ -1,1 +1,1 @@\n-a\n+b\n"}, exec_ctx)
    assert not ok and "rename" in out["error"]


def test_apply_patch_jail_escape_refused(workspace, exec_ctx):
    from nimna.tools import default_registry
    out, ok = _run(default_registry(), "apply_patch",
                   {"diff": "--- ../evil.txt\n+++ ../evil.txt\n@@ -1,1 +1,1 @@\n-a\n+b\n"},
                   exec_ctx)
    assert not ok and "jail" in out["error"]
    assert not (workspace.parent / "evil.txt").exists()


def test_apply_patch_oversize_result_refused(workspace, exec_ctx):
    (workspace / "grow.txt").write_text("seed\n", encoding="utf-8")
    diff = ("--- a/grow.txt\n+++ b/grow.txt\n@@ -1,1 +1,2 @@\n seed\n"
            "+" + "y" * (EDIT_MAX_BYTES + 1) + "\n")
    from nimna.tools import default_registry
    out, ok = _run(default_registry(), "apply_patch", {"diff": diff}, exec_ctx)
    assert not ok and "exceeds" in out["error"]
    assert (workspace / "grow.txt").read_text() == "seed\n"          # untouched


def test_core_apply_edit_is_shared_by_both_doors(workspace):
    target = workspace / "core.txt"
    target.write_text("keep A keep\n", encoding="utf-8")
    out = apply_edit(target, "A", "B")
    assert out["replaced"] == 1 and target.read_text() == "keep B keep\n"
    with pytest.raises(edit_mod.ToolError, match="ambiguous"):
        target.write_text("x x\n", encoding="utf-8")
        apply_edit(target, "x", "y")           # expect_once default


# ------------------------------------------------------------ Fabric door ---
def test_bound_agent_routes_edit_file_through_fabric(workspace, settings, provider):
    from nimna.core.agent import Agent
    from nimna.core.approval import DeferToClient
    from nimna.memory import MemoryStore
    from nimna.skills import SkillManager
    from nimna.tools import default_registry
    gateway = make_gateway(_fabric_registry(workspace), workspace)
    agent = Agent(provider, SkillManager(__import__("pathlib").Path(__file__)
                  .resolve().parent.parent / "skills"), default_registry(),
                  MemoryStore(":memory:"), settings,
                  approval_policy=DeferToClient(), workspace=workspace,
                  execution_gateway=gateway)
    legacy = agent.tools.get("edit_file")
    with mock.patch.object(type(legacy), "run",
                           side_effect=AssertionError("BYPASS!")) as spy:
        provider.queue(
            '{"skills": ["code_execution"], "reason": "r"}',
            ModelResponse(text="", tool_calls=[ToolCall(
                name="edit_file",
                arguments={"path": "notes.txt", "old_text": "hello",
                           "new_text": "salam"})]),
        )
        # confirm-vocabulary gate suspends BEFORE routing (B-gate); the resume
        # then routes through the FABRIC (policy → authorization → evidence).
        r1 = agent.run("probe", session_id="t8f")
        assert r1.status == RunStatus.AWAITING_APPROVAL
        provider.queue("done")
        r2 = agent.resume(r1.run_id, True)
        assert r2.status == RunStatus.DONE
    assert spy.call_count == 0                                # legacy door never touched
    assert (workspace / "notes.txt").read_text() == "salam\nworld\n"
    record = r2.tool_calls[0]
    assert record.ok is True and '"status": "EDITED"' in record.result_preview
    entries = gateway.evidence.entries                        # hash-chained evidence
    assert any(e.get("tool_id") == "edit_file" and e.get("hash") for e in entries)


def test_fabric_policy_deny_refuses_edit_file_before_handler(workspace):
    from nimna.execution.policy import Effect, Policy, PolicyRule
    reg = _fabric_registry(workspace)
    deny = Policy("deny-edits", "1.0.0", rules=(
        PolicyRule("deny-edit-file", Effect.DENY, resource_patterns=("edit*",)),))
    gateway = make_gateway(reg, workspace, policy=deny)
    outcome = gateway.invoke_for_agent(
        "edit_file", {"path": "notes.txt", "old_text": "hello", "new_text": "x"},
        actor="agent-under-test", session_id="t8p", mission_id="t8p",
        resource="notes.txt")
    assert outcome.execution_status == "POLICY_DENIED"
    assert outcome.ok is False and outcome.result is None     # handler never ran
    assert (workspace / "notes.txt").read_text().startswith("hello")   # untouched
    assert any(e.get("tool_id") == "edit_file" for e in gateway.evidence.entries)


def test_fabric_apply_patch_routed_and_evidenced(workspace):
    _seed_code(workspace)
    reg = _fabric_registry(workspace)
    gateway = make_gateway(reg, workspace)
    outcome = gateway.invoke_for_agent(
        "apply_patch", {"diff": PATCH},
        actor="agent-under-test", session_id="t8x", mission_id="t8x",
        resource="code.txt")
    assert outcome.ok is True and outcome.execution_status == "EXECUTED"
    assert (workspace / "code.txt").read_text().startswith("line1\nline-two\n")
    assert outcome.record and outcome.record.get("hash")


def test_fabric_input_schema_refuses_extra_arguments_fail_closed(workspace):
    """المخطط صارم: مفتاح زائد ⇒ INPUT_INVALID بلا معالج (لا صمت)."""
    reg = _fabric_registry(workspace)
    gateway = make_gateway(reg, workspace)
    outcome = gateway.invoke_for_agent(
        "edit_file", {"path": "notes.txt", "old_text": "hello", "new_text": "x",
                      "purpose": "extra-key"},
        actor="agent-under-test", session_id="t8s", mission_id="t8s",
        resource="notes.txt")
    assert outcome.execution_status == "INPUT_INVALID" and outcome.ok is False
    assert outcome.result is None
    assert (workspace / "notes.txt").read_text().startswith("hello")   # untouched
