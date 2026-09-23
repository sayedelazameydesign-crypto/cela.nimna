"""Capability + Policy primitive (P1-T6) — deterministic governance over the
Tool Registry (P1-T5), never a policy engine inside it.

Ticket P1-T6 in ``docs/architecture/agent-platform-audit.md``. The separation
is the contract:

* **Capability ≠ Permission** — a tool *requires* capabilities; permission is
  the outcome of Policy evaluation → Authorization. Nothing here ever reads
  "the model thought this was safe".
* **Deterministic** — the same inputs produce the same decision, always:
  first-match rule iteration over an ordered, validated rule set.
* **Default DENY, unbreakable** — unknown capability → DENY, revoked
  capability → DENY, missing policy rule → DENY, missing/expired/wrong-actor/
  wrong-tool authorization → DENY, malformed request → DENY, invalid context →
  DENY, policy *error* → DENY (fail-closed). There is no ``fallback = allow``.
* **No capability amplification** — ``shell.read`` never implies
  ``shell.execute``. T6 ships **no implicit capability inheritance**; a partial
  match between granted and required capabilities authorizes nothing.
* **Resource scoping** — rules speak ``ALLOW <capability> ON <pattern>`` and
  workspace resources go through the T2 observation boundary
  (:func:`resolve_inside_workspace`): resolve → containment, never a string
  prefix check. ``../../`` escapes are DENY before any rule is consulted.
* **Authorization ≠ Policy** — Policy answers "is this *kind* of operation
  allowed?"; Authorization answers "is *this* execution authorized *now*?".
  ``Policy → ALLOW`` with ``Authorization → EXPIRED`` is a final DENY.
  ``REQUIRE_CONFIRMATION`` is a first-class effect: it proceeds only when a
  valid consent grant exists, and it is recorded as what it is.
"""
from __future__ import annotations

import enum
import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .observation import ScopeError, resolve_inside_workspace

__all__ = [
    "Effect", "PolicyDecision", "PolicyInput", "PolicyRule", "Policy",
    "CapabilityCatalog", "AuthorizationGrant", "Authorizer",
    "adjudicate", "t5_capability_resolver", "t5_policy_adapter", "t5_authorizer_adapter",
    "PolicyError", "WorkspaceBoundary",
]

CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

MAX_RULES = 64
MAX_CONTEXT_KEYS = 64
MAX_CONTEXT_BYTES = 65_536


class PolicyError(Exception):
    """The policy itself is malformed — the caller must fail closed."""


class Effect(str, enum.Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


@dataclass(frozen=True)
class PolicyDecision:
    """The outcome of a deterministic policy evaluation (never an impression)."""
    effect: Effect
    reason: str
    policy_id: str
    policy_version: str
    matched_rule: str

    @property
    def allowed(self) -> bool:
        """Gate-passthrough: only DENY blocks at the policy layer. Confirmation
        still has to clear the authorization (consent) gate."""
        return self.effect is not Effect.DENY

    def to_dict(self) -> dict[str, Any]:
        return {"effect": self.effect.value, "reason": self.reason,
                "policy_id": self.policy_id, "policy_version": self.policy_version,
                "matched_rule": self.matched_rule}


# --------------------------------------------------------------------------- #
# request + workspace boundary
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyInput:
    """Everything a policy may look at — and nothing else (no free-text trust)."""
    actor: str
    tool_id: str
    capabilities: tuple[str, ...]              # capabilities the tool REQUIRES
    requested_operation: str
    resource: str
    context: Mapping[str, Any] = field(default_factory=dict)
    risk: str = "LOW"
    authorization: Optional["AuthorizationGrant"] = None
    state: Mapping[str, Any] = field(default_factory=dict)

    def validate_problems(self) -> list[str]:
        problems: list[str] = []
        if not isinstance(self.actor, str) or not self.actor.strip():
            problems.append("actor is required")
        if not isinstance(self.tool_id, str) or not self.tool_id.strip():
            problems.append("tool_id is required")
        if not isinstance(self.capabilities, tuple) or not all(
            isinstance(cap, str) and cap for cap in self.capabilities
        ):
            problems.append("capabilities must be a tuple of names")
        if not isinstance(self.requested_operation, str) or not self.requested_operation.strip():
            problems.append("requested_operation is required")
        if not isinstance(self.resource, str) or not self.resource.strip():
            problems.append("resource is required")
        if ".." in self.resource:
            problems.append("resource must not contain traversal segments")
        if not isinstance(self.context, Mapping):
            problems.append("context must be a mapping")
        elif len(self.context) > MAX_CONTEXT_KEYS or len(str(self.context)) > MAX_CONTEXT_BYTES:
            problems.append("context exceeds bounded size")
        if not isinstance(self.state, Mapping):
            problems.append("state must be a mapping")
        return problems


class WorkspaceBoundary:
    """Resource containment anchored in the T2 observation boundary.

    ``workspace`` / ``workspace/<rel>`` resources are resolved with
    :func:`resolve_inside_workspace` (resolve → containment, never a string
    prefix check); escapes raise before any rule is consulted. Any other
    namespaced resource is treated as an opaque identifier (matched by rule
    patterns only) — it can never touch the filesystem through this layer.
    """

    def __init__(self, workspace_root) -> None:
        self.workspace_root = workspace_root

    def check(self, resource: str) -> None:
        """Raise :class:`ScopeError` if a workspace resource escapes the jail."""
        if resource == "workspace" or resource.startswith("workspace/"):
            resolve_inside_workspace(self.workspace_root,
                                     "." if resource == "workspace" else resource[len("workspace/"):])
        # non-workspace identifiers are opaque strings here — no filesystem meaning


# --------------------------------------------------------------------------- #
# rules + policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    effect: Effect
    capabilities: frozenset[str] = frozenset()   # empty = wildcard
    operation: str = "*"                          # exact or "*"
    resource_patterns: tuple[str, ...] = ("*",)   # fnmatch, case-sensitive
    note: str = ""

    def validate(self) -> None:
        if not RULE_ID_RE.match(self.rule_id or ""):
            raise PolicyError(f"rule_id {self.rule_id!r} invalid")
        if not isinstance(self.effect, Effect):
            raise PolicyError(f"{self.rule_id}: effect must be an Effect")
        if self.operation != "*" and (not isinstance(self.operation, str) or not self.operation.strip()):
            raise PolicyError(f"{self.rule_id}: operation invalid")
        if not isinstance(self.capabilities, frozenset) or not all(
            isinstance(cap, str) and CAPABILITY_NAME_RE.match(cap) for cap in self.capabilities
        ):
            raise PolicyError(f"{self.rule_id}: capabilities invalid")
        if not isinstance(self.resource_patterns, tuple) or not self.resource_patterns or not all(
            isinstance(pat, str) and pat and ".." not in pat for pat in self.resource_patterns
        ):
            raise PolicyError(f"{self.rule_id}: resource_patterns invalid")

    def matches(self, input_: PolicyInput) -> bool:
        if self.capabilities and not (set(self.capabilities) & set(input_.capabilities)):
            return False
        if self.operation != "*" and self.operation != input_.requested_operation:
            return False
        return any(fnmatch.fnmatchcase(input_.resource, pat) for pat in self.resource_patterns)


@dataclass(frozen=True)
class Policy:
    """An ordered, validated rule set. First match wins — deterministic."""
    policy_id: str
    version: str
    rules: tuple[PolicyRule, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not RULE_ID_RE.match(self.policy_id or ""):
            raise PolicyError(f"policy_id {self.policy_id!r} invalid")
        if not VERSION_RE.match(self.version or ""):
            raise PolicyError(f"{self.policy_id}: version must be MAJOR.MINOR.PATCH")
        if not isinstance(self.rules, tuple) or not (1 <= len(self.rules) <= MAX_RULES):
            raise PolicyError(f"{self.policy_id}: needs 1..{MAX_RULES} rules")
        seen: set[str] = set()
        for rule in self.rules:
            rule.validate()
            if rule.rule_id in seen:
                raise PolicyError(f"{self.policy_id}: duplicate rule_id {rule.rule_id!r}")
            seen.add(rule.rule_id)

    def evaluate(self, input_: PolicyInput, *, boundary: Optional[WorkspaceBoundary] = None) -> PolicyDecision:
        """Deterministic evaluation. Every failure mode is fail-closed."""
        deny = lambda reason, rule: PolicyDecision(Effect.DENY, reason, self.policy_id, self.version, rule)
        try:
            problems = input_.validate_problems()
            if problems:
                return deny(f"malformed request: {'; '.join(problems[:3])}", "malformed-request")
            if boundary is not None:
                try:
                    boundary.check(input_.resource)
                except ScopeError as exc:
                    return deny(f"resource outside the workspace boundary: {str(exc)[:150]}",
                                "workspace-boundary")
            for rule in self.rules:                    # first match wins, in order
                if rule.matches(input_):
                    return PolicyDecision(rule.effect, rule.note or f"rule {rule.rule_id}",
                                          self.policy_id, self.version, rule.rule_id)
            return deny("no rule matched (default-deny)", "default-deny")
        except Exception as exc:  # noqa: BLE001 — policy errors must fail closed (M9)
            return deny(f"policy error (fail-closed): {exc.__class__.__name__}", "policy-error")


# --------------------------------------------------------------------------- #
# capability resolver — no inheritance, no amplification
# --------------------------------------------------------------------------- #
class CapabilityCatalog:
    """Which capabilities exist and which are revoked — nothing implied."""

    def __init__(self, known: Iterable[str], revoked: Iterable[str] = ()) -> None:
        self.known = frozenset(known)
        self.revoked = frozenset(revoked)
        unknown_revoked = self.revoked - self.known
        if unknown_revoked:
            raise PolicyError(f"revoked capabilities not in catalog: {sorted(unknown_revoked)}")

    def resolve(self, required: Iterable[str]) -> tuple[str, ...]:
        """Validate the capabilities a tool requires. Exact set — no expansion,
        no inheritance, no normalization (M8/M10 never have a hole to stand in)."""
        names = tuple(required)
        for name in names:
            if name not in self.known:
                raise ValueError(f"unknown capability {name!r} (default-deny)")
            if name in self.revoked:
                raise ValueError(f"revoked capability {name!r}")
        return names


# --------------------------------------------------------------------------- #
# authorization — "is THIS execution authorized NOW?"
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AuthorizationGrant:
    actor: str
    tool_id: str
    policy_version: str
    expires_at: Optional[datetime] = None      # None = does not expire
    consent: bool = False                      # REQUIRE_CONFIRMATION needs explicit consent

    def validate_problems(self) -> list[str]:
        problems: list[str] = []
        if not isinstance(self.actor, str) or not self.actor.strip():
            problems.append("grant actor is required")
        if not isinstance(self.tool_id, str) or not self.tool_id.strip():
            problems.append("grant tool_id is required")
        if not isinstance(self.policy_version, str) or not VERSION_RE.match(self.policy_version or ""):
            problems.append("grant policy_version must be MAJOR.MINOR.PATCH")
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            problems.append("expires_at must be a datetime or None")
        return problems


@dataclass(frozen=True)
class AuthorizationDecision:
    granted: bool
    reason: str
    matched_rule: str = "authorization"


class Authorizer:
    """Deterministic grant validation. Absent/expired/wrong-actor/wrong-tool/
    version-mismatch are all DENY — there is no lenient mode."""

    def check(self, grant: Optional[AuthorizationGrant], *, actor: str, tool_id: str,
              policy_version: str, now: Optional[datetime] = None) -> AuthorizationDecision:
        if grant is None:
            return AuthorizationDecision(False, "no authorization grant (default-deny)")
        problems = grant.validate_problems()
        if problems:
            return AuthorizationDecision(False, f"malformed grant: {'; '.join(problems[:3])}",
                                         "authorization-malformed")
        if grant.actor != actor:
            return AuthorizationDecision(False, f"grant actor {grant.actor!r} != requester {actor!r}",
                                         "authorization-actor")
        if grant.tool_id != tool_id:
            return AuthorizationDecision(False, f"grant tool {grant.tool_id!r} != requested {tool_id!r}",
                                         "authorization-tool")
        if grant.policy_version != policy_version:
            return AuthorizationDecision(
                False, f"grant consented to policy {grant.policy_version}, current is {policy_version}",
                "authorization-policy-version")
        if grant.expires_at is not None:
            moment = now or datetime.now(timezone.utc)
            expires = grant.expires_at if grant.expires_at.tzinfo else grant.expires_at.replace(tzinfo=timezone.utc)
            if moment > expires:
                return AuthorizationDecision(False, f"authorization expired at {expires.isoformat()}",
                                             "authorization-expired")
        return AuthorizationDecision(True, "grant valid", "authorization-valid")


# --------------------------------------------------------------------------- #
# full T6 pipeline (standalone; does not reimplement T5)
# --------------------------------------------------------------------------- #
def adjudicate(policy: Policy, catalog: CapabilityCatalog, authorizer: Authorizer,
               input_: PolicyInput, *, boundary: Optional[WorkspaceBoundary] = None,
               now: Optional[datetime] = None) -> PolicyDecision:
    """Resolver → Policy → Authorization for one request. Final word, fail-closed.

    ``REQUIRE_CONFIRMATION`` survives ONLY when the attached grant is valid AND
    carries explicit consent; otherwise it is a DENY — never a silent ALLOW."""
    try:
        catalog.resolve(input_.capabilities)
    except ValueError as exc:
        return PolicyDecision(Effect.DENY, str(exc), policy.policy_id, policy.version,
                              "capability-catalog")
    decision = policy.evaluate(input_, boundary=boundary)
    if decision.effect is Effect.DENY:
        return decision
    grant = input_.authorization
    needs_consent = decision.effect is Effect.REQUIRE_CONFIRMATION
    authorization = authorizer.check(grant, actor=input_.actor, tool_id=input_.tool_id,
                                     policy_version=policy.version, now=now)
    if not authorization.granted:
        return PolicyDecision(Effect.DENY,
                              f"policy {decision.matched_rule} but authorization refused: {authorization.reason}",
                              policy.policy_id, policy.version, authorization.matched_rule)
    if needs_consent and not (grant is not None and grant.consent):
        return PolicyDecision(Effect.DENY,
                              f"rule {decision.matched_rule} requires confirmation and the grant carries no consent",
                              policy.policy_id, policy.version, "confirmation-consent")
    return decision


# --------------------------------------------------------------------------- #
# adapters INTO the P1-T5 invoke pipeline (T5 stays untouched)
# --------------------------------------------------------------------------- #
def t5_capability_resolver(catalog: CapabilityCatalog):
    """A T5 ``capability_resolver`` callable backed by the catalog. Raises
    ValueError on unknown/revoked — T5's gate records it as CAPABILITY_DENIED."""
    def _resolver(descriptor):
        return catalog.resolve(descriptor.capabilities)
    return _resolver


def t5_policy_adapter(policy: Policy, *, boundary: Optional[WorkspaceBoundary] = None,
                      stats: Optional[dict[str, int]] = None):
    """A T5 ``policy`` callable backed by a T6 Policy. DENY blocks; ALLOW and
    REQUIRE_CONFIRMATION pass the policy gate — confirmation still has to clear
    T5's authorization gate, and the policy identity lands in the evidence reason."""
    def _policy(descriptor, arguments):
        from .tool_registry import PolicyDecision as T5Decision
        input_ = PolicyInput(
            actor="registry-invocation",
            tool_id=descriptor.tool_id,
            capabilities=tuple(descriptor.capabilities),
            requested_operation=arguments.get("operation") or descriptor.tool_id,
            resource=str(arguments.get("resource") or "workspace"),
            context={"risk": descriptor.risk_level},
            risk=descriptor.risk_level,
        )
        decision = policy.evaluate(input_, boundary=boundary)
        if stats is not None:
            stats[decision.effect.value] = stats.get(decision.effect.value, 0) + 1
        return T5Decision(
            allowed=decision.allowed,
            reason=(f"{decision.effect.value}|{decision.policy_id}@{decision.policy_version}"
                    f"|{decision.matched_rule}: {decision.reason}"))
    return _policy


def t5_authorizer_adapter(authorizer: Authorizer, *, actor: str, policy_version_of, grant_for=None):
    """A T5 ``authorizer`` callable backed by the T6 Authorizer. ``grant_for``
    (descriptor, arguments) -> Optional[AuthorizationGrant]; the policy version
    is read per-invocation via ``policy_version_of(descriptor, arguments)`` so a
    version mismatch is a DENY at the gate, not a crash."""
    def _authorizer(descriptor, arguments):
        from .tool_registry import AuthorizationDecision as T5Authorization
        grant = grant_for(descriptor, arguments) if grant_for else None
        decision = authorizer.check(grant, actor=actor,
                                    tool_id=descriptor.tool_id,
                                    policy_version=policy_version_of(descriptor, arguments))
        return T5Authorization(granted=decision.granted, reason=decision.reason)
    return _authorizer
