"""Tests for the Agent Execution Gateway (P1-T7).

Owner's contract: single execution path · no-bypass invariant (Policy=DENY ⇒
handler_called=False) · gateway owns no policy · unified evidence record ·
full fabric scenario Agent→Registry→Capability→Policy→Authorization→Execute→
Observe→Verify→Checkpoint→Evidence · refusals stop before the handler.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nimna.execution.gateway import ExecutionGateway, InvocationContext
from nimna.execution.policy import (
    AuthorizationGrant,
    Authorizer,
    CapabilityCatalog,
    Effect,
    Policy,
    PolicyRule,
)
from nimna.execution.recovery import CheckpointStore
from nimna.execution.tool_registry import ToolDescriptor, ToolRegistry

POLICY_VERSION = "1.0.0"
SCHEMAS = (
    {"type": "object",
     "properties": {"command": {"type": "string", "maxLength": 200}},
     "required": ["command"], "additionalProperties": False},
    {"type": "object",
     "properties": {"status": {"type": "string"}, "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"}},
     "required": ["status"]},
)


def make_policy() -> Policy:
    return Policy("gateway-policy", POLICY_VERSION, rules=(
        PolicyRule("deny-system", Effect.DENY, capabilities=frozenset({"shell"}),
                   resource_patterns=("system/*",)),
        PolicyRule("allow-workspace-shell", Effect.ALLOW, capabilities=frozenset({"shell"}),
                   resource_patterns=("workspace", "workspace/*", "workspace/**"),
                   note="sandboxed shell inside the workspace"),
    ))


@pytest.fixture
def fabric(tmp_path: Path):
    """The whole fabric wired exactly as production would: registry, catalog,
    policy, authorizer, workspace boundary, checkpoint store, evidence chain."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "checkpoints").mkdir()          # store outside the observed jail
    calls: list[str] = []                        # the handler spy

    def handler(arguments: dict) -> dict:
        calls.append(str(arguments.get("command", "")))
        (workspace / "out.txt").write_text("gateway-42", encoding="utf-8")
        return {"status": "SUCCESS", "exit_code": 0, "timed_out": False}

    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        tool_id="sandbox.command", version="1.2.0",
        input_schema=SCHEMAS[0], output_schema=SCHEMAS[1],
        capabilities=("shell",), side_effects=("process", "filesystem"),
        risk_level="HIGH", handler=handler))
    gateway = ExecutionGateway(
        registry,
        catalog=CapabilityCatalog(known={"shell"}),
        policy=make_policy(),
        authorizer=Authorizer(),
        workspace_root=workspace,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints"),
        actor="agent-1",
    )
    return gateway, calls, workspace


def grant(**overrides) -> AuthorizationGrant:
    fields = dict(actor="agent-1", tool_id="sandbox.command", policy_version=POLICY_VERSION)
    fields.update(overrides)
    return AuthorizationGrant(**fields)


def context(**overrides) -> InvocationContext:
    fields = dict(
        actor="agent-1", session_id="s1", mission_id="m1",
        granted_capabilities=("shell",), authorization=grant(),
        requested_operation="shell.execute", resource="workspace/out.txt",
        verify_spec=({"kind": "file_exists", "path": "out.txt"},
                     {"kind": "content_matches", "path": "out.txt", "contains": "gateway-42"}),
    )
    fields.update(overrides)
    return InvocationContext(**fields)


# --------------------------------------------------------------------------- #
# the full fabric scenario (owner's integration test)
# --------------------------------------------------------------------------- #
def test_full_fabric_scenario_end_to_end(fabric):
    gateway, calls, workspace = fabric
    outcome = gateway.invoke("sandbox.command", {"command": "produce out.txt"}, context())
    assert outcome.ok is True and outcome.handler_called is True
    assert outcome.decision == "ALLOW" and outcome.execution_status == "EXECUTED"
    assert calls == ["produce out.txt"]                       # the handler ran exactly once
    # Observe (T2)
    assert outcome.observation["changes"][0]["kind"] == "CREATED"
    assert outcome.observation["changes"][0]["path"] == "out.txt"
    assert outcome.observation["before_root_hash"] != outcome.observation["after_root_hash"]
    # Verify (T3, deterministic)
    assert outcome.verification["verdict"] == "PASS"
    assert outcome.verification["summary"]["passed"] == 2
    # Checkpoint (T4)
    assert outcome.checkpoint["state"] == "COMPLETED"
    assert outcome.checkpoint["checkpoint_id"].startswith("ckpt_")
    # unified evidence (all owner fields, in one record)
    record = outcome.record
    assert record["stage"] == "gateway"
    assert record["tool_id"] == "sandbox.command" and record["tool_version"] == "1.2.0"
    assert record["request_digest"].startswith("sha256:")
    assert record["capabilities"] == ["shell"]
    assert record["policy_id"] == "gateway-policy" and record["policy_version"] == POLICY_VERSION
    assert record["authorization_id"].startswith("sha256:")
    assert record["decision"] == "ALLOW" and record["execution_status"] == "EXECUTED"
    assert record["observation"]["delta_changes"] == 1
    assert record["verification"]["verdict"] == "PASS"
    assert record["checkpoint"]["state"] == "COMPLETED"
    assert record["hash"].startswith("sha256:")               # evidence_digest
    # the chain itself verifies, and the underlying T5 gate event is recorded too
    assert gateway.evidence.verify() is True
    decisions = [e.get("decision") for e in gateway.evidence.entries]
    assert "EXECUTED" in decisions and "ALLOW" in decisions   # gate event + gateway record


# --------------------------------------------------------------------------- #
# refusals stop BEFORE the handler (the owner's four scenarios)
# --------------------------------------------------------------------------- #
def test_refusals_stop_before_the_handler(fabric):
    gateway, calls, _ = fabric
    scenarios = {
        "missing capability": context(granted_capabilities=()),
        "policy DENY": context(resource="system/db"),
        "expired authorization": context(
            authorization=grant(expires_at=__import__("datetime").datetime(
                2020, 1, 1, tzinfo=__import__("datetime").timezone.utc))),
        "outside workspace": context(resource="outside-zone/db"),
    }
    expected_status = {
        "missing capability": "CAPABILITY_DENIED",
        "policy DENY": "POLICY_DENIED",
        "expired authorization": "AUTHORIZATION_DENIED",
        "outside workspace": "POLICY_DENIED",
    }
    for name, ctx in scenarios.items():
        outcome = gateway.invoke("sandbox.command", {"command": f"should-not-run:{name}"}, ctx)
        # the explicit invariant: DENY ⇒ handler_called = False
        assert outcome.handler_called is False, name
        assert outcome.ok is False and calls == [], name      # spy: handler never ran
        assert outcome.decision == "DENY", name
        assert outcome.execution_status == expected_status[name], name
        assert outcome.verification is None and outcome.checkpoint is None, name
    # every refusal left an honest unified record
    records = [e for e in gateway.evidence.entries if e.get("stage") == "gateway"]
    assert len(records) == 4
    assert all(r["decision"] == "DENY" and r["handler_called"] is False for r in records)
    assert gateway.evidence.verify() is True


def test_disabled_and_unknown_tools_refused(fabric):
    gateway, calls, _ = fabric
    from nimna.execution.tool_registry import LifecycleState
    outcome = gateway.invoke("phantom.command", {}, context())
    assert outcome.execution_status == "NOT_FOUND" and outcome.handler_called is False
    # disable the real tool through the registry lifecycle, then try the gateway
    registry = gateway._registry                              # test-only access
    registry.set_availability("sandbox.command", LifecycleState.DISABLED)
    outcome2 = gateway.invoke("sandbox.command", {"command": "x"}, context())
    assert outcome2.execution_status == "DISABLED" and outcome2.handler_called is False
    assert calls == []


def test_malformed_context_refused(fabric):
    gateway, calls, _ = fabric
    outcome = gateway.invoke("sandbox.command", {"command": "x"},
                             context(actor=""))
    assert outcome.ok is False and outcome.execution_status == "CONTEXT_INVALID"
    assert outcome.handler_called is False and calls == []
    outcome2 = gateway.invoke("sandbox.command", {"command": "x"},
                              context(verify_spec=({"kind": "file_exists"},) * 100))
    assert outcome2.execution_status == "CONTEXT_INVALID"
    # traversal resources are caught at the context gate — even earlier than
    # the workspace boundary, still fail-closed with the handler untouched
    outcome3 = gateway.invoke("sandbox.command", {"command": "x"},
                              context(resource="workspace/../../etc/shadow"))
    assert outcome3.execution_status == "CONTEXT_INVALID"
    assert outcome3.handler_called is False and calls == []


# --------------------------------------------------------------------------- #
# honesty of the fabric tail
# --------------------------------------------------------------------------- #
def test_handler_error_is_honest_and_checkpoints_failed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "ckpt").mkdir()
    def boom(arguments):
        raise RuntimeError("disk on fire")
    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        tool_id="sandbox.command", version="1.0.0", input_schema=SCHEMAS[0],
        output_schema=SCHEMAS[1], capabilities=("shell",),
        side_effects=("process", "filesystem"), handler=boom))
    gateway = ExecutionGateway(registry, catalog=CapabilityCatalog(known={"shell"}),
                               policy=make_policy(), authorizer=Authorizer(),
                               workspace_root=workspace,
                               checkpoint_store=CheckpointStore(tmp_path / "ckpt"))
    outcome = gateway.invoke("sandbox.command", {"command": "explode"}, context(mission_id="m-err"))
    assert outcome.ok is False and outcome.handler_called is True      # it ran and failed
    assert outcome.execution_status == "HANDLER_ERROR"
    assert outcome.checkpoint["state"] == "FAILED"
    record = outcome.record
    assert record["execution_status"] == "HANDLER_ERROR" and record["decision"] == "DENY"


def test_verification_fail_blocks_ok_and_checkpoints_failed(fabric):
    gateway, calls, _ = fabric
    ctx = context(verify_spec=({"kind": "file_exists", "path": "never-created.txt"},))
    outcome = gateway.invoke("sandbox.command", {"command": "make out.txt"}, ctx)
    assert outcome.handler_called is True                       # side effects happened
    assert outcome.verification["verdict"] == "FAIL"
    assert outcome.ok is False                                  # never fabricated as success
    assert outcome.checkpoint["state"] == "FAILED"


def test_no_verify_spec_stays_checkpointed_diagnosable(fabric):
    gateway, calls, _ = fabric
    outcome = gateway.invoke("sandbox.command", {"command": "make out.txt"},
                             context(verify_spec=()))
    assert outcome.ok is True and outcome.verification is None
    assert outcome.checkpoint["state"] == "CHECKPOINTED"        # never claimed complete


# --------------------------------------------------------------------------- #
# structural no-bypass: the agent has no public path to a raw handler
# --------------------------------------------------------------------------- #
def test_structural_no_bypass_public_surface(fabric):
    gateway, _, _ = fabric
    public = {name for name in dir(gateway) if not name.startswith("_")}
    # nothing on the public surface executes a handler directly
    assert "invoke" in public                                   # the single entry point
    for name in public:
        assert "handler" not in name and "execute_handler" not in name
    # discovery is redacted — no handler reference ever leaves the registry
    listed = gateway.list_tools()
    assert listed and all("handler" not in entry for entry in listed)
    assert listed[0]["tool_id"] == "sandbox.command"
    assert callable(getattr(gateway, "_registry", None).__class__)  # private, mangled-ish


# --------------------------------------------------------------------------- #
# the real agent binding (opt-in; default path untouched)
# --------------------------------------------------------------------------- #
def test_agent_binding_routes_through_the_gateway(workspace, settings, provider):
    """With a gateway bound, the agent's tool call runs the whole fabric; the
    legacy handler is never touched (spy raises if it were)."""
    from nimna.core.agent import Agent
    from nimna.core.approval import AutoApprove
    from nimna.core.state import RunStatus
    from nimna.memory import MemoryStore
    from nimna.providers.base import ModelResponse, ToolCall
    from nimna.skills import SkillManager
    from nimna.tools import default_registry

    REPO = Path(__file__).resolve().parent.parent
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        tool_id="list_files", version="1.0.0",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object",
                       "properties": {"status": {"type": "string"}, "note": {"type": "string"}},
                       "required": ["status"]},
        capabilities=(), side_effects=("none",),
        handler=lambda args: calls.append("ran") or {"status": "SUCCESS", "note": "via-gateway"}))
    agent_policy = Policy("agent-policy", POLICY_VERSION, rules=(
        PolicyRule("allow-all-registered", Effect.ALLOW, resource_patterns=("*",)),
    ))
    gateway = ExecutionGateway(registry, catalog=CapabilityCatalog(known=set()),
                               policy=agent_policy, authorizer=Authorizer(),
                               workspace_root=workspace,
                               checkpoint_store=CheckpointStore(workspace.parent / "ckpt"),
                               actor="agent-under-test")
    agent = Agent(provider, SkillManager(REPO / "skills"), default_registry(),
                  MemoryStore(":memory:"), settings, approval_policy=AutoApprove(),
                  workspace=settings.workspace_dir)
    agent.execution_gateway = gateway
    # sabotage the legacy implementation — if the agent bypassed the gateway this raises
    legacy = agent.tools.get("list_files")
    import unittest.mock as mock
    with mock.patch.object(type(legacy), "run", side_effect=AssertionError("BYPASS!")):
        provider.queue(
            '{"skills": [], "reason": "none"}',
            ModelResponse(text="", tool_calls=[ToolCall(name="list_files", arguments={})]),
            "done",
        )
        result = agent.run("سرد الملفات", session_id="gw-s1")
    assert result.status == RunStatus.DONE
    assert calls == ["ran"]                                     # executed via the fabric
    records = [e for e in gateway.evidence.entries if e.get("stage") == "gateway"]
    assert len(records) == 1
    record = records[0]
    assert record["tool_id"] == "list_files" and record["decision"] == "ALLOW"
    assert record["actor"] == "agent-under-test"
    assert record["policy_id"] == "agent-policy"
    assert gateway.evidence.verify() is True


def test_agent_without_gateway_keeps_the_legacy_path(workspace, settings, provider):
    """Default (no gateway bound) is byte-for-byte the legacy behaviour."""
    from nimna.core.agent import Agent
    from nimna.core.approval import AutoApprove
    from nimna.core.state import RunStatus
    from nimna.memory import MemoryStore
    from nimna.providers.base import ModelResponse, ToolCall
    from nimna.skills import SkillManager
    from nimna.tools import default_registry

    REPO = Path(__file__).resolve().parent.parent
    agent = Agent(provider, SkillManager(REPO / "skills"), default_registry(),
                  MemoryStore(":memory:"), settings, approval_policy=AutoApprove(),
                  workspace=settings.workspace_dir)
    assert agent.execution_gateway is None
    provider.queue(
        '{"skills": [], "reason": "none"}',
        ModelResponse(text="", tool_calls=[ToolCall(name="list_files", arguments={})]),
        "done",
    )
    result = agent.run("سرد الملفات", session_id="legacy-s1")
    assert result.status == RunStatus.DONE
    assert result.tool_calls[0].name == "list_files"
    assert result.tool_calls[0].ok is True                      # legacy path intact
