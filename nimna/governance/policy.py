"""Central tool policy vocabulary.

The current tool registry uses ``safe`` and ``confirm`` for backwards
compatibility.  This module adds the richer risk vocabulary from the Agent OS
blueprint without weakening the existing approval gate.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class ToolRisk(str, Enum):
    SAFE = "SAFE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    SECRET = "SECRET"


class PolicyVerdict(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyVerdict
    risk: ToolRisk
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is not PolicyVerdict.DENY


@dataclass
class PolicyEngine:
    """Conservative policy evaluator for tool authorization.

    ``allowed_tools`` is the capability scope produced by the skill loader.  A
    tool outside that scope is denied before validation or execution.  Tools
    marked ``confirm`` are mapped to MEDIUM and therefore need the existing
    ApprovalPolicy; restricted actions can be explicitly denied.
    """

    deny_tools: set[str] | None = None
    require_approval_at_or_above: ToolRisk = ToolRisk.MEDIUM

    def evaluate(
        self,
        *,
        tool_name: str,
        declared_risk: str = "safe",
        allowed_tools: Iterable[str] | None = None,
        contains_secret: bool = False,
        explicit_consent: bool = False,
    ) -> PolicyResult:
        if allowed_tools is not None and tool_name not in set(allowed_tools):
            return PolicyResult(PolicyVerdict.DENY, ToolRisk.HIGH, "tool is outside the active capability scope")
        if self.deny_tools and tool_name in self.deny_tools:
            return PolicyResult(PolicyVerdict.DENY, ToolRisk.HIGH, "tool is denied by policy")
        if contains_secret:
            risk = ToolRisk.SECRET
        else:
            mapping = {"safe": ToolRisk.SAFE, "confirm": ToolRisk.MEDIUM, "restricted": ToolRisk.HIGH}
            risk = mapping.get(str(declared_risk).lower(), ToolRisk.HIGH)
        order = list(ToolRisk)
        threshold_risk = self.require_approval_at_or_above
        if not isinstance(threshold_risk, ToolRisk):
            threshold_risk = ToolRisk(str(threshold_risk).upper())
        threshold = order.index(threshold_risk)
        if order.index(risk) >= threshold and not explicit_consent:
            return PolicyResult(PolicyVerdict.APPROVAL_REQUIRED, risk, "explicit user approval is required")
        return PolicyResult(PolicyVerdict.ALLOW, risk, "policy checks passed")


__all__ = ["PolicyVerdict", "PolicyEngine", "PolicyResult", "ToolRisk"]
