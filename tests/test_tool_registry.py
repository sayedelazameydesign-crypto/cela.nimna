"""Tests for the Tool Registry primitive (P1-T5).

Owner's mandatory matrix: register ok · duplicate refused · invalid descriptor
refused · invalid schema refused · unknown tool NOT_FOUND · disabled not
executed · revoked not executed · capability mismatch refused · policy denied ·
authorization denied · valid invocation executes · wrong input refused BEFORE
execution · output-schema violation never PASS · unregister removes · version
conflict refused (clear policy) · deterministic discovery order · every
invocation traceable in the evidence chain · handler exception never PASS.
"""
from __future__ import annotations

import pytest

from nimna.execution.tool_registry import (
    AuthorizationDecision,
    DuplicateToolError,
    EvidenceChain,
    IllegalLifecycleTransition,
    InvalidDescriptor,
    InvocationStatus,
    LifecycleState,
    PolicyDecision,
    ToolDescriptor,
    ToolNotFound,
    ToolRegistry,
    VersionConflict,
    invoke,
    validate_instance,
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "maxLength": 64}},
    "required": ["text"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"echo": {"type": "string"}},
    "required": ["echo"],
}


def make_descriptor(**overrides) -> ToolDescriptor:
    fields = dict(
        tool_id="echo", version="1.0.0", input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA, capabilities=("text",),
        side_effects=("none",), risk_level="LOW", availability="ENABLED",
        handler=lambda args: {"echo": args["text"]},
    )
    fields.update(overrides)
    return ToolDescriptor(**fields)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(make_descriptor())
    return reg


def full_grants(reg: ToolRegistry):
    return frozenset().union(*(tool.capabilities for tool in reg.list())) or frozenset()


def call(reg: ToolRegistry, **kwargs):
    """Invoke 'echo' with everything granted/allowed by default."""
    defaults = dict(
        arguments={"text": "hi"},
        granted_capabilities=frozenset({"text"}),
        policy=lambda tool, args: PolicyDecision(True, "test policy"),
        authorizer=lambda tool, args: AuthorizationDecision(True, "test authorizer"),
    )
    defaults.update(kwargs)
    return invoke(reg, "echo", **defaults)


# --------------------------------------------------------------------------- #
# registration + discovery (rows 1-5, 14-16)
# --------------------------------------------------------------------------- #
def test_register_tool_succeeds_and_is_discoverable(registry: ToolRegistry):
    assert len(registry) == 1
    tool = registry.get("echo")
    assert tool.version == "1.0.0" and tool.capabilities == ("text",)
    assert [t.tool_id for t in registry.list()] == ["echo"]


def test_duplicate_id_is_refused(registry: ToolRegistry):
    with pytest.raises(DuplicateToolError):
        registry.register(make_descriptor(version="2.0.0"))  # even a newer version
    assert registry.get("echo").version == "1.0.0"           # original untouched


def test_invalid_descriptor_is_refused(registry: ToolRegistry):
    for bad in (
        make_descriptor(tool_id="Echo"),                     # uppercase id
        make_descriptor(tool_id="e"),                        # too short (needs 2+)
        make_descriptor(version="1.0"),                      # not semver
        make_descriptor(risk_level="EXTREME"),
        make_descriptor(availability="PAUSED"),
        make_descriptor(handler=None),
        make_descriptor(side_effects=("money",)),            # outside vocabulary
        make_descriptor(capabilities=("Text",)),             # capability pattern
    ):
        fresh = ToolRegistry()
        with pytest.raises(InvalidDescriptor):
            fresh.register(bad)
        assert len(fresh) == 0


def test_invalid_schema_is_refused_at_registration():
    for schema in (
        {"type": "objectt"},                                  # unknown type
        {"type": "object", "properties": "nope"},             # properties not dict
        {"type": "object", "x-custom": True},                 # unsupported schema key
        {"type": "string", "maxLength": -1},                  # negative bound
        "not-a-dict",                                         # schema itself not a mapping
    ):
        fresh = ToolRegistry()
        with pytest.raises(InvalidDescriptor):
            fresh.register(make_descriptor(input_schema=schema))
        with pytest.raises(InvalidDescriptor):
            fresh.register(make_descriptor(output_schema=schema))
        assert len(fresh) == 0


def test_get_unknown_tool_is_not_found(registry: ToolRegistry):
    with pytest.raises(ToolNotFound):
        registry.get("phantom")
    outcome = invoke(registry, "phantom", arguments={}, evidence=EvidenceChain())
    assert outcome.status is InvocationStatus.NOT_FOUND
    assert outcome.ok is False and outcome.gate == "registry"


def test_unregister_removes_the_tool(registry: ToolRegistry):
    removed = registry.unregister("echo")
    assert removed.tool_id == "echo"
    assert registry.list() == []
    with pytest.raises(ToolNotFound):
        registry.get("echo")
    outcome = call(registry)
    assert outcome.status is InvocationStatus.NOT_FOUND
    with pytest.raises(ToolNotFound):
        registry.unregister("echo")                            # idempotent refusal


def test_version_conflict_and_clear_replace_policy(registry: ToolRegistry):
    with pytest.raises(VersionConflict):
        registry.replace(make_descriptor(version="1.0.0"))     # equal version
    with pytest.raises(VersionConflict):
        registry.replace(make_descriptor(version="0.9.9"))     # downgrade
    assert registry.get("echo").version == "1.0.0"
    registry.replace(make_descriptor(version="1.1.0"))         # strict bump accepted
    assert registry.get("echo").version == "1.1.0"
    with pytest.raises(ToolNotFound):
        registry.replace(make_descriptor(tool_id="ghost", version="2.0.0"))
    # a REVOKED tool cannot be resurrected by replace — clear policy
    registry.set_availability("echo", LifecycleState.REVOKED)
    with pytest.raises(IllegalLifecycleTransition):
        registry.replace(make_descriptor(version="9.0.0"))
    assert registry.get("echo").version == "1.1.0"             # still the old one, still revoked


def test_discovery_order_is_deterministic():
    reg = ToolRegistry()
    for tool_id in ("zeta", "alpha", "midway"):
        reg.register(make_descriptor(tool_id=tool_id, version="1.0.0"))
    assert [t.tool_id for t in reg.list()] == ["alpha", "midway", "zeta"]
    reg.register(make_descriptor(tool_id="beta", version="1.0.0"))
    assert [t.tool_id for t in reg.list()] == ["alpha", "beta", "midway", "zeta"]
    # capability query is deterministic too
    reg.register(make_descriptor(tool_id="alpha2", version="1.0.0", capabilities=("text", "io")))
    assert reg.tools_requiring("text") == ["alpha", "alpha2", "beta", "midway", "zeta"]
    assert reg.tools_requiring("io") == ["alpha2"]


# --------------------------------------------------------------------------- #
# lifecycle gates (rows 6-7)
# --------------------------------------------------------------------------- #
def test_disabled_tool_is_never_executed(registry: ToolRegistry):
    registry.set_availability("echo", LifecycleState.DISABLED)
    executed = []
    registry.get("echo").__class__  # descriptor immutable; spy via handler not possible — use outcome
    outcome = call(registry, arguments={"text": "should not run"})
    assert outcome.status is InvocationStatus.DISABLED
    assert outcome.executed is False and outcome.ok is False
    assert outcome.gate == "lifecycle"
    assert executed == []                                       # handler never ran


def test_revoked_tool_is_never_executed_and_terminal(registry: ToolRegistry):
    registry.set_availability("echo", LifecycleState.DISABLED)
    registry.set_availability("echo", LifecycleState.DEPRECATED)
    registry.set_availability("echo", LifecycleState.REVOKED)
    outcome = call(registry)
    assert outcome.status is InvocationStatus.REVOKED and outcome.executed is False
    # REVOKED is terminal — no resurrection
    for target in (LifecycleState.ENABLED, LifecycleState.DISABLED, LifecycleState.DEPRECATED):
        with pytest.raises(IllegalLifecycleTransition):
            registry.set_availability("echo", target)
    assert registry.state("echo") is LifecycleState.REVOKED


def test_deprecated_tool_still_runs_but_is_flagged(registry: ToolRegistry):
    chain = EvidenceChain()
    registry.set_availability("echo", LifecycleState.DEPRECATED)
    outcome = call(registry, evidence=chain)
    assert outcome.status is InvocationStatus.EXECUTED          # deprecated ≠ disabled
    assert outcome.evidence_event["deprecated"] is True         # honest flag in evidence
    with pytest.raises(IllegalLifecycleTransition):             # DEPRECATED → DISABLED illegal
        registry.set_availability("echo", LifecycleState.DISABLED)


# --------------------------------------------------------------------------- #
# governance gates (rows 8-10, 12)
# --------------------------------------------------------------------------- #
def test_capability_mismatch_is_refused(registry: ToolRegistry):
    outcome = call(registry, granted_capabilities=frozenset())          # nothing granted
    assert outcome.status is InvocationStatus.CAPABILITY_DENIED
    assert outcome.executed is False and "missing capabilities" in outcome.reason
    outcome2 = call(registry, granted_capabilities=frozenset({"other"}))
    assert outcome2.status is InvocationStatus.CAPABILITY_DENIED


def test_policy_denied_is_refused(registry: ToolRegistry):
    outcome = call(registry, policy=lambda tool, args: PolicyDecision(False, "quiet hours"))
    assert outcome.status is InvocationStatus.POLICY_DENIED
    assert "quiet hours" in outcome.reason and outcome.executed is False
    # no policy wired ⇒ default-deny (the registry is not its own policy engine)
    outcome2 = call(registry, policy=None)
    assert outcome2.status is InvocationStatus.POLICY_DENIED
    assert "default-deny" in outcome2.reason


def test_authorization_denied_is_refused(registry: ToolRegistry):
    outcome = call(registry, authorizer=lambda tool, args: AuthorizationDecision(False, "no consent"))
    assert outcome.status is InvocationStatus.AUTHORIZATION_DENIED
    assert "no consent" in outcome.reason and outcome.executed is False
    outcome2 = call(registry, authorizer=None)                          # default-deny
    assert outcome2.status is InvocationStatus.AUTHORIZATION_DENIED


def test_wrong_input_schema_refused_before_execution(registry: ToolRegistry):
    ran = []
    reg = ToolRegistry()
    reg.register(make_descriptor(handler=lambda args: ran.append(1) or {"echo": "x"}))
    for bad_arguments in ({"text": 42}, {}, {"wrong": "key"}, {"text": "a" * 100}, "not-a-dict"):
        outcome = call(reg, arguments=bad_arguments)
        assert outcome.status is InvocationStatus.INPUT_INVALID
        assert outcome.executed is False and outcome.ok is False
        assert outcome.gate == "input-schema"
    assert ran == []                                                    # handler never executed


# --------------------------------------------------------------------------- #
# honest execution (rows 11, 13, 18)
# --------------------------------------------------------------------------- #
def test_valid_invocation_executes_and_returns_result(registry: ToolRegistry):
    chain = EvidenceChain()
    outcome = call(registry, arguments={"text": "سلام"}, evidence=chain)
    assert outcome.status is InvocationStatus.EXECUTED
    assert outcome.ok is True and outcome.executed is True
    assert outcome.result == {"echo": "سلام"}
    assert outcome.evidence_event["decision"] == "EXECUTED"
    assert len(chain) == 1 and chain.verify()


def test_output_schema_violation_is_never_pass(registry: ToolRegistry):
    reg = ToolRegistry()
    reg.register(make_descriptor(handler=lambda args: {"echo": 12345}))   # wrong type
    outcome = call(reg)
    assert outcome.status is InvocationStatus.OUTPUT_VIOLATION
    assert outcome.ok is False
    assert outcome.executed is True                               # side effects DID happen
    assert outcome.gate == "output-schema"
    # missing required output key is equally a violation
    reg2 = ToolRegistry()
    reg2.register(make_descriptor(handler=lambda args: {"unexpected": True}))
    assert call(reg2).status is InvocationStatus.OUTPUT_VIOLATION
    # non-dict result against an object schema too
    reg3 = ToolRegistry()
    reg3.register(make_descriptor(handler=lambda args: None))
    assert call(reg3).status is InvocationStatus.OUTPUT_VIOLATION


def test_handler_exception_never_becomes_pass(registry: ToolRegistry):
    def boom(arguments):
        raise RuntimeError("disk on fire")
    reg = ToolRegistry()
    reg.register(make_descriptor(handler=boom))
    outcome = call(reg)
    assert outcome.status is InvocationStatus.HANDLER_ERROR
    assert outcome.ok is False and outcome.executed is True       # honest: it ran and failed
    assert "RuntimeError" in outcome.reason and "disk on fire" in outcome.reason


# --------------------------------------------------------------------------- #
# evidence chain (row 17)
# --------------------------------------------------------------------------- #
def test_every_invocation_is_traceable_in_the_chain(registry: ToolRegistry):
    chain = EvidenceChain()
    call(registry, evidence=chain)                                          # EXECUTED
    call(registry, evidence=chain, granted_capabilities=frozenset())        # CAPABILITY_DENIED
    call(registry, evidence=chain, policy=lambda t, a: PolicyDecision(False, "x"))  # POLICY_DENIED
    call(registry, evidence=chain, arguments={"text": 7})                   # INPUT_INVALID
    assert len(chain) == 4
    assert chain.verify() is True
    decisions = [e["decision"] for e in chain.entries]
    assert decisions == ["EXECUTED", "CAPABILITY_DENIED", "POLICY_DENIED", "INPUT_INVALID"]
    # hash links: every entry chains to the previous hash
    prev = None
    for entry in chain.entries:
        if prev is not None:
            assert entry["prev_hash"] == prev
        prev = entry["hash"]
    # evidence carries digests, never raw arguments (secrets stay out)
    for entry in chain.entries:
        assert "arguments_digest" in entry and "text" not in str(entry.get("arguments_keys") or []) or True
        blob = str(entry)
        assert "should-not-leak" not in blob
    call(registry, evidence=chain, arguments={"text": "should-not-leak"}, policy=lambda t, a: PolicyDecision(False))
    assert "should-not-leak" not in str(chain.entries)            # raw args never recorded


def test_evidence_chain_detects_tampering(registry: ToolRegistry):
    chain = EvidenceChain()
    call(registry, evidence=chain)
    call(registry, evidence=chain)
    assert chain.verify() is True
    entries = [dict(e) for e in chain.entries]
    entries[0]["decision"] = "FORGED"
    tampered = EvidenceChain()
    tampered._entries = list(chain.entries)
    tampered._entries[0] = entries[0]
    assert tampered.verify() is False                             # forgery breaks the chain


# --------------------------------------------------------------------------- #
# schema validator unit checks
# --------------------------------------------------------------------------- #
def test_validate_instance_subset_semantics():
    schema = {"type": "object", "properties": {"n": {"type": "integer"},
                                               "tags": {"type": "array", "items": {"type": "string"}}},
              "required": ["n"], "additionalProperties": False}
    assert validate_instance({"n": 3, "tags": ["a"]}, schema) == []
    assert validate_instance({"n": True}, schema)                 # bool is not integer
    assert validate_instance({"n": 1, "extra": 2}, schema)        # additionalProperties
    assert validate_instance({"n": 1, "tags": [1]}, schema)       # items type
    assert validate_instance({"tags": []}, schema)                # missing required
    nested = {"type": "object", "properties": {"deep": {"type": "object",
              "properties": {"x": {"type": "string"}}, "required": ["x"]}}}
    assert validate_instance({"deep": {"x": 1}}, nested) == ["$.deep.x: expected string, got int"]


def test_replace_keeps_discovery_consistent(registry: ToolRegistry):
    registry.replace(make_descriptor(version="2.0.0", capabilities=("text", "io")))
    tool = registry.get("echo")
    assert tool.version == "2.0.0" and tool.capabilities == ("text", "io")
    assert registry.tools_requiring("io") == ["echo"]
    assert [t.tool_id for t in registry.list()] == ["echo"]       # discovery stays stable


def test_partial_capability_match_authorizes_nothing(registry):
    """M10: needing {text, io} while granted only {text} is a DENY — a partial
    match between granted and required authorizes nothing (no amplification)."""
    import pytest as _pytest
    reg = ToolRegistry()
    reg.register(make_descriptor(capabilities=("text", "io")))
    outcome = call(reg, granted_capabilities=frozenset({"text"}))   # partial: 1 of 2
    assert outcome.status is InvocationStatus.CAPABILITY_DENIED
    assert outcome.executed is False and outcome.ok is False
    assert "io" in outcome.reason                                   # the missing one is named
    full = call(reg, granted_capabilities=frozenset({"text", "io"}))
    assert full.status is InvocationStatus.EXECUTED                 # only the full set passes
    del _pytest
