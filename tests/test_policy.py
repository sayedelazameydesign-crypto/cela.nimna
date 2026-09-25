"""Tests for Capability + Policy (P1-T6).

Owner's mandatory matrix: default deny · exact capability allowed · missing
capability denied · extra capabilities grant nothing · unknown capability
denied · policy deny/allow · REQUIRE_CONFIRMATION · authorization absent/
expired/wrong-actor/wrong-tool denied · resource inside/outside scope · policy
version mismatch denied · malformed policy/request denied · revoked capability
denied · capability amplification denied · deterministic decisions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nimna.execution.observation import ScopeError
from nimna.execution.policy import (
    AuthorizationGrant,
    Authorizer,
    CapabilityCatalog,
    Effect,
    Policy,
    PolicyError,
    PolicyInput,
    PolicyRule,
    WorkspaceBoundary,
    adjudicate,
)

POLICY_VERSION = "1.0.0"
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


def make_policy(**overrides) -> Policy:
    """ALLOW shell.execute ON workspace/** ; explicit DENY first for .git and system."""
    rules = (
        PolicyRule("deny-system", Effect.DENY, capabilities=frozenset({"shell"}),
                   operation="*", resource_patterns=("system/*", "/etc/*")),
        PolicyRule("deny-git-writes", Effect.DENY, capabilities=frozenset({"file.write"}),
                   operation="*", resource_patterns=("workspace/.git/*",)),
        PolicyRule("allow-workspace-shell", Effect.ALLOW, capabilities=frozenset({"shell"}),
                   operation="*", resource_patterns=("workspace", "workspace/*", "workspace/**"),
                   note="sandboxed shell inside the workspace"),
    )
    rules = overrides.get("rules", rules)
    return Policy(policy_id="suite-policy", version=POLICY_VERSION, rules=rules)


def make_input(**overrides) -> PolicyInput:
    fields = dict(
        actor="agent-1", tool_id="sandbox.command",
        capabilities=("shell",), requested_operation="shell.execute",
        resource="workspace/src/output.txt",
        authorization=AuthorizationGrant(actor="agent-1", tool_id="sandbox.command",
                                         policy_version=POLICY_VERSION),
    )
    fields.update(overrides)
    return PolicyInput(**fields)


def make_catalog(**overrides) -> CapabilityCatalog:
    return CapabilityCatalog(known=overrides.get("known", {"shell", "file.write", "shell.read"}),
                             revoked=overrides.get("revoked", ()))


def grant(**overrides) -> AuthorizationGrant:
    fields = dict(actor="agent-1", tool_id="sandbox.command", policy_version=POLICY_VERSION)
    fields.update(overrides)
    return AuthorizationGrant(**fields)


# --------------------------------------------------------------------------- #
# policy layer
# --------------------------------------------------------------------------- #
def test_default_deny_when_no_rule_matches():
    decision = make_policy().evaluate(make_input(resource="unknown-zone/x"))
    assert decision.effect is Effect.DENY and decision.matched_rule == "default-deny"
    covered = make_policy().evaluate(make_input(resource="system/db"))
    assert covered.effect is Effect.DENY and covered.matched_rule == "deny-system"


def test_exact_capability_and_inside_scope_allowed():
    decision = make_policy().evaluate(make_input())
    assert decision.effect is Effect.ALLOW
    assert decision.matched_rule == "allow-workspace-shell"
    assert decision.policy_id == "suite-policy" and decision.policy_version == POLICY_VERSION


def test_missing_capability_never_matches_rule():
    # tool requires nothing the rule governs → no match → default DENY
    decision = make_policy().evaluate(make_input(capabilities=(), resource="workspace/x"))
    assert decision.effect is Effect.DENY and decision.matched_rule == "default-deny"


def test_extra_capabilities_grant_nothing_extra():
    # a rule governing ONLY shell must not fire for a tool requesting file.write
    decision = make_policy().evaluate(make_input(capabilities=("file.write",),
                                                 resource="workspace/src/main.py"))
    assert decision.effect is Effect.DENY and decision.matched_rule == "default-deny"


def test_unknown_capability_is_denied_by_catalog():
    catalog = make_catalog()
    with pytest.raises(ValueError, match="unknown capability"):
        catalog.resolve(("shell.execute",))            # not in the catalog → DENY path


def test_policy_deny_rule_wins_by_order():
    policy = make_policy(rules=(
        PolicyRule("allow-all-first", Effect.ALLOW, operation="*",
                   resource_patterns=("workspace/*",)),
        PolicyRule("deny-git-later", Effect.DENY, capabilities=frozenset({"file.write"}),
                   resource_patterns=("workspace/.git/*",)),
    ))
    decision = policy.evaluate(make_input(capabilities=("file.write",),
                                          resource="workspace/.git/config"))
    assert decision.effect is Effect.ALLOW and decision.matched_rule == "allow-all-first"
    # ordered the other way, the DENY fires first — deterministic, documented order
    ordered = make_policy(rules=(
        PolicyRule("deny-git-first", Effect.DENY, capabilities=frozenset({"file.write"}),
                   resource_patterns=("workspace/.git/*",)),
        PolicyRule("allow-all-after", Effect.ALLOW, operation="*",
                   resource_patterns=("workspace/*",)),
    ))
    decision2 = ordered.evaluate(make_input(capabilities=("file.write",),
                                            resource="workspace/.git/config"))
    assert decision2.effect is Effect.DENY and decision2.matched_rule == "deny-git-first"


def test_require_confirmation_is_first_class():
    policy = make_policy(rules=(
        PolicyRule("confirm-dangerous", Effect.REQUIRE_CONFIRMATION,
                   capabilities=frozenset({"shell"}), resource_patterns=("workspace/secrets/*",),
                   note="human must confirm"),
    ))
    decision = policy.evaluate(make_input(resource="workspace/secrets/key.txt"))
    assert decision.effect is Effect.REQUIRE_CONFIRMATION
    assert decision.matched_rule == "confirm-dangerous"
    assert decision.allowed is True            # passes the POLICY gate…
    # …but adjudication still demands an explicit consent grant
    final = adjudicate(policy, make_catalog(), Authorizer(),
                       make_input(resource="workspace/secrets/key.txt",
                                  authorization=grant(consent=False)),
                       now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "confirmation-consent"
    ok = adjudicate(policy, make_catalog(), Authorizer(),
                    make_input(resource="workspace/secrets/key.txt",
                               authorization=grant(consent=True)),
                    now=NOW)
    assert ok.effect is Effect.REQUIRE_CONFIRMATION


def test_malformed_policy_is_refused_and_never_evaluates():
    bad_policies = (
        lambda: Policy("bad policy", POLICY_VERSION, (PolicyRule("r1", Effect.ALLOW),)),  # id space
        lambda: Policy("p", "1.0", (PolicyRule("r1", Effect.ALLOW),)),                    # version
        lambda: Policy("p", POLICY_VERSION, ()),                                          # no rules
        lambda: Policy("p", POLICY_VERSION, (PolicyRule("r1", Effect.ALLOW),
                                             PolicyRule("r1", Effect.DENY))),             # duplicate id
        lambda: Policy("p", POLICY_VERSION, (PolicyRule("r1", "ALLOW"),)),                # raw string effect
        lambda: Policy("p", POLICY_VERSION, (PolicyRule("r1", Effect.ALLOW,
                                                    resource_patterns=("../etc/*",)),)),  # traversal pattern
    )
    for build in bad_policies:
        with pytest.raises(PolicyError):
            policy = build()
            policy.evaluate(make_input())


def test_policy_error_is_fail_closed_not_allow():
    # a rule that explodes mid-evaluation must DENY, never ALLOW (M9)
    class ExplodingRule(PolicyRule):
        def matches(self, input_):
            raise RuntimeError("boom")
    policy = Policy("p", POLICY_VERSION, (ExplodingRule("boom", Effect.ALLOW),))
    decision = policy.evaluate(make_input())
    assert decision.effect is Effect.DENY and decision.matched_rule == "policy-error"
    assert "fail-closed" in decision.reason
    final = adjudicate(policy, make_catalog(), Authorizer(), make_input(), now=NOW)
    assert final.effect is Effect.DENY


def test_malformed_request_is_denied():
    for bad_input in (
        make_input(actor=""),
        make_input(tool_id=None),
        make_input(capabilities=("shell",) if False else "shell"),   # not a tuple
        make_input(resource="workspace/../../etc/passwd"),
        make_input(context="not-a-mapping"),
        make_input(requested_operation=""),
    ):
        decision = make_policy().evaluate(bad_input)
        assert decision.effect is Effect.DENY
        assert decision.matched_rule in {"malformed-request", "workspace-boundary"}


# --------------------------------------------------------------------------- #
# resource scoping through the T2 boundary
# --------------------------------------------------------------------------- #
def test_resource_inside_scope_allowed_outside_denied(tmp_path):
    boundary = WorkspaceBoundary(tmp_path)
    (tmp_path / "src").mkdir()
    inside = make_policy().evaluate(make_input(resource="workspace/src/output.txt"), boundary=boundary)
    assert inside.effect is Effect.ALLOW
    traversal = make_policy().evaluate(make_input(resource="workspace/../../etc/shadow"), boundary=boundary)
    assert traversal.effect is Effect.DENY and traversal.matched_rule == "malformed-request"
    # a symlink pointing outside is the stealth escape — the T2 resolve() catches it
    outside_dir = tmp_path.parent / "policy-escape-target"
    outside_dir.mkdir(exist_ok=True)
    (tmp_path / "leak").symlink_to(outside_dir)
    escaped = make_policy().evaluate(make_input(resource="workspace/leak/secret"), boundary=boundary)
    assert escaped.effect is Effect.DENY and escaped.matched_rule == "workspace-boundary"
    # non-workspace opaque resources only match rule patterns textually
    system_resource = make_policy().evaluate(make_input(resource="system/db"), boundary=boundary)
    assert system_resource.effect is Effect.DENY and system_resource.matched_rule == "deny-system"


def test_boundary_uses_t2_containment_not_prefix(tmp_path):
    boundary = WorkspaceBoundary(tmp_path)
    # a sibling directory whose name starts with the workspace's name must NOT pass
    sibling = tmp_path.parent / (tmp_path.name + "-evil")
    sibling.mkdir(exist_ok=True)
    with pytest.raises(ScopeError):
        boundary.check(f"workspace/{sibling}")
    # traversal is caught by input validation AND by the boundary
    with pytest.raises(ScopeError):
        boundary.check("workspace/../outside-evil")
    decision = make_policy().evaluate(make_input(resource=f"workspace/{sibling}"), boundary=boundary)
    assert decision.effect is Effect.DENY and decision.matched_rule == "workspace-boundary"


# --------------------------------------------------------------------------- #
# authorization layer — Policy says nothing without a valid grant
# --------------------------------------------------------------------------- #
def test_policy_allow_with_expired_authorization_is_final_deny():
    expired = grant(expires_at=NOW - timedelta(minutes=5))
    final = adjudicate(make_policy(), make_catalog(), Authorizer(),
                       make_input(authorization=expired), now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "authorization-expired"


def test_authorization_absent_denied():
    final = adjudicate(make_policy(), make_catalog(), Authorizer(),
                       make_input(authorization=None), now=NOW)
    assert final.effect is Effect.DENY and "no authorization grant" in final.reason


def test_authorization_wrong_actor_denied():
    final = adjudicate(make_policy(), make_catalog(), Authorizer(),
                       make_input(authorization=grant(actor="agent-2")), now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "authorization-actor"


def test_authorization_wrong_tool_denied():
    final = adjudicate(make_policy(), make_catalog(), Authorizer(),
                       make_input(authorization=grant(tool_id="other.tool")), now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "authorization-tool"


def test_policy_version_mismatch_denied():
    final = adjudicate(make_policy(), make_catalog(), Authorizer(),
                       make_input(authorization=grant(policy_version="0.9.0")), now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "authorization-policy-version"


def test_malformed_grant_denied():
    final = adjudicate(make_policy(), make_catalog(), Authorizer(),
                       make_input(authorization=grant(policy_version="latest")), now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "authorization-malformed"


def test_valid_authorization_completes_allow():
    final = adjudicate(make_policy(), make_catalog(), Authorizer(),
                       make_input(authorization=grant(expires_at=NOW + timedelta(hours=1))), now=NOW)
    assert final.effect is Effect.ALLOW and final.matched_rule == "allow-workspace-shell"


# --------------------------------------------------------------------------- #
# capability catalog — amplification is impossible by construction
# --------------------------------------------------------------------------- #
def test_capability_amplification_is_denied():
    """shell.read granted must NEVER unlock shell.execute — no implicit inheritance."""
    catalog = make_catalog()
    # the resolver expands nothing — exact declared set, shell.read implies nothing
    assert catalog.resolve(("shell.read",)) == ("shell.read",)
    assert catalog.resolve(("shell.read", "file.write")) == ("shell.read", "file.write")
    with pytest.raises(ValueError):
        catalog.resolve(("shell.execute",))            # unknown AND never implied by shell.read
    # and the T6 pipeline with granted=shell.read semantics: the policy rule
    # requires the tool to DECLARE the capability; declared shell + granted
    # shell.read is T5's exact-subset gate — a partial match authorizes nothing.
    final = adjudicate(make_policy(), catalog, Authorizer(),
                       make_input(capabilities=("shell.execute",),
                                  authorization=grant()), now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "capability-catalog"


def test_revoked_capability_is_denied():
    catalog = make_catalog(revoked={"shell"})
    with pytest.raises(ValueError, match="revoked"):
        catalog.resolve(("shell",))
    final = adjudicate(make_policy(), catalog, Authorizer(), make_input(), now=NOW)
    assert final.effect is Effect.DENY and final.matched_rule == "capability-catalog"


def test_catalog_rejects_revoked_unknown_names():
    with pytest.raises(PolicyError):
        CapabilityCatalog(known={"shell"}, revoked={"phantom"})


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_same_inputs_same_decision():
    policy, catalog, authorizer, boundary = make_policy(), make_catalog(), Authorizer(), WorkspaceBoundary("/tmp")
    inputs = [
        make_input(),
        make_input(resource="system/db"),
        make_input(resource="workspace/../../etc/passwd"),
        make_input(authorization=grant(actor="nope")),
    ]
    for input_ in inputs:
        first = adjudicate(policy, catalog, authorizer, input_, boundary=boundary, now=NOW)
        for _ in range(3):
            assert adjudicate(policy, catalog, authorizer, input_, boundary=boundary, now=NOW) == first


# --------------------------------------------------------------------------- #
# T5 adapters — governance plugs into the registry without touching it
# --------------------------------------------------------------------------- #
def test_adapters_wire_t6_into_t5_invoke(tmp_path):
    from nimna.execution.policy import (
        t5_capability_resolver,
        t5_policy_adapter,
    )
    from nimna.execution.tool_registry import (
        AuthorizationDecision as T5Authorization,
    )
    from nimna.execution.tool_registry import (
        EvidenceChain,
        InvocationStatus,
        ToolDescriptor,
        ToolRegistry,
        invoke,
    )
    (tmp_path / "src").mkdir()
    catalog = make_catalog()
    policy = make_policy()
    stats: dict[str, int] = {}
    reg = ToolRegistry()
    reg.register(ToolDescriptor(
        tool_id="sandbox.command", version="1.0.0",
        input_schema={"type": "object", "properties": {"command": {"type": "string"}},
                      "required": ["command"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"status": {"type": "string"}},
                       "required": ["status"]},
        capabilities=("shell",), risk_level="HIGH",
        handler=lambda args: {"status": "SUCCESS"}))
    chain = EvidenceChain()
    outcome = invoke(
        reg, "sandbox.command", {"command": "echo hi"},   # resource defaults to "workspace"
        granted_capabilities=frozenset({"shell"}),
        capability_resolver=t5_capability_resolver(catalog),
        policy=t5_policy_adapter(policy, boundary=WorkspaceBoundary(tmp_path), stats=stats),
        authorizer=lambda d, a: T5Authorization(True, "operator consent"),
        evidence=chain,
    )
    assert outcome.status is InvocationStatus.EXECUTED
    assert stats == {"ALLOW": 1}
    assert "ALLOW|suite-policy@1.0.0|allow-workspace-shell" in outcome.evidence_event["policy_reason"]
    assert chain.verify()

    # unknown capability ⇒ resolver refusal ⇒ CAPABILITY_DENIED with evidence
    bad_catalog = CapabilityCatalog(known={"file.write"})
    outcome2 = invoke(
        reg, "sandbox.command", {"command": "echo hi"},
        granted_capabilities=frozenset({"shell"}),
        capability_resolver=t5_capability_resolver(bad_catalog),
        policy=t5_policy_adapter(policy), authorizer=lambda d, a: T5Authorization(True, "ok"),
        evidence=chain,
    )
    assert outcome2.status is InvocationStatus.CAPABILITY_DENIED
    assert "unknown capability" in outcome2.reason
    assert len(chain) == 2 and chain.verify()
