"""Agent Execution Gateway (P1-T7) — the single crossing point between an
agent and the Execution Fabric.

Ticket P1-T7 in ``docs/architecture/agent-platform-audit.md``. The contract:

* **Single execution path** — ``ExecutionGateway.invoke(tool_id, request,
  context)`` is the only way an agent executes a tool. There is no public
  accessor to a raw handler: the registry is held privately (``_registry``)
  and discovery returns redacted descriptors without handlers.
* **No bypass** — Agent ✗→ raw handler / shell handler / file handler;
  Agent ✓→ Gateway. The gateway itself never executes a handler outside the
  P1-T5 gated pipeline (lifecycle → input schema → capability → policy →
  authorization → execute → output schema), so every guarantee of T5/T6 is
  structural, not optional.
* **The gateway owns no policy** — it is an orchestrator: Registry →
  Capability (T6 catalog) → Policy (T6) → Authorization (T6) → Executor,
  all injected at construction. No God Object.
* **Unified evidence** — every invocation leaves ONE record with: tool_id,
  tool_version, request_digest, capabilities, policy_id, policy_version,
  authorization_id, decision, execution_status, observation, verification,
  checkpoint, evidence_digest — digests only, never raw secrets.
* **Invariant** — ``Policy = DENY ⇒ handler_called = False``. Refusals stop
  before the handler and are still recorded honestly.
* **Fabric tail** — after an executed call the gateway observes the filesystem
  (T2 boundary), verifies (T3 deterministic verifier), and checkpoints (T4
  recovery store) — one provenance record per invocation, bounded.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .observation import FilesystemDelta, WorkspaceObserver
from .policy import (
    AuthorizationGrant,
    Authorizer,
    CapabilityCatalog,
    Policy,
    WorkspaceBoundary,
    t5_authorizer_adapter,
    t5_capability_resolver,
    t5_policy_adapter,
)
from .recovery import CheckpointStore, RecoveryManager, RecoveryState
from .tool_registry import (
    EvidenceChain,
    InvocationStatus,
    ToolDescriptor,
    ToolRegistry,
    invoke as _registry_invoke,
)
from .verification import DeterministicVerifier

__all__ = ["InvocationContext", "GatewayOutcome", "ExecutionGateway"]

MAX_REQUEST_BYTES = 262_144
MAX_VERIFY_SPEC_CHECKS = 32


class GatewayContextError(ValueError):
    """Malformed invocation context — refused before anything executes."""


@dataclass(frozen=True)
class InvocationContext:
    """Who is asking, under which grant, with which verification demands."""
    actor: str
    session_id: str = ""
    mission_id: str = ""
    granted_capabilities: tuple[str, ...] = ()
    authorization: Optional[AuthorizationGrant] = None
    requested_operation: str = ""
    resource: str = "workspace"
    verify_spec: tuple[dict[str, Any], ...] = ()

    def validate_problems(self) -> list[str]:
        problems: list[str] = []
        if not isinstance(self.actor, str) or not self.actor.strip():
            problems.append("actor is required")
        if not isinstance(self.granted_capabilities, tuple):
            problems.append("granted_capabilities must be a tuple")
        if not isinstance(self.resource, str) or not self.resource.strip():
            problems.append("resource is required")
        if ".." in self.resource:
            problems.append("resource must not contain traversal segments")
        if not isinstance(self.verify_spec, tuple) or len(self.verify_spec) > MAX_VERIFY_SPEC_CHECKS:
            problems.append(f"verify_spec must be a tuple of at most {MAX_VERIFY_SPEC_CHECKS} checks")
        if self.authorization is not None and not isinstance(self.authorization, AuthorizationGrant):
            problems.append("authorization must be an AuthorizationGrant")
        return problems


@dataclass
class GatewayOutcome:
    """What actually happened — honest by construction, never fabricated."""
    tool_id: str
    ok: bool
    handler_called: bool
    decision: str                       # ALLOW / DENY / REQUIRE_CONFIRMATION
    execution_status: str               # InvocationStatus value at the registry gate
    reason: str
    result: Any = None
    invocation_id: str = ""
    observation: Optional[dict[str, Any]] = None
    verification: Optional[dict[str, Any]] = None
    checkpoint: Optional[dict[str, Any]] = None
    record: Optional[dict[str, Any]] = None       # the unified evidence record
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"tool_id": self.tool_id, "ok": self.ok, "handler_called": self.handler_called,
                "decision": self.decision, "execution_status": self.execution_status,
                "reason": self.reason, "invocation_id": self.invocation_id,
                "observation": self.observation, "verification": self.verification,
                "checkpoint": self.checkpoint, "duration_ms": self.duration_ms,
                "evidence_digest": (self.record or {}).get("hash", "")}


def _digest(payload: Any, limit: int = MAX_REQUEST_BYTES) -> str:
    try:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return "unserializable"
    if len(body) > limit:
        return "oversized"
    return "sha256:" + hashlib.sha256(body).hexdigest()


class ExecutionGateway:
    """Orchestrator only: Registry → Capability → Policy → Authorization →
    Executor → Observe → Verify → Checkpoint → Evidence."""

    def __init__(self, registry: ToolRegistry, *, catalog: CapabilityCatalog,
                 policy: Policy, authorizer: Authorizer, workspace_root: Path,
                 checkpoint_store: CheckpointStore,
                 evidence: Optional[EvidenceChain] = None,
                 actor: str = "agent",
                 compat_tools: tuple[str, ...] = ()) -> None:
        self._registry = registry              # private: no public handler access
        self._catalog = catalog
        self._policy = policy
        self._authorizer = authorizer
        self._workspace = Path(workspace_root).resolve()
        self._boundary = WorkspaceBoundary(self._workspace)
        self._checkpoints = checkpoint_store
        self.evidence = evidence if evidence is not None else EvidenceChain()
        self.actor = actor
        # EXPLICIT compatibility classification (T7.1): legacy-named tools that
        # may keep running on the old path WHILE this gateway is bound. Empty by
        # default ⇒ a bound gateway refuses every unregistered tool (fail-closed).
        self.compat_tools = frozenset(compat_tools)
        self._policy_stats: dict[str, int] = {}
        self._mission_seq = 0
        self._recovery: dict[str, RecoveryManager] = {}

    # -- discovery (redacted — handlers never leave the registry) ---------- #
    def has(self, tool_id: str) -> bool:
        try:
            self._registry.get(tool_id)
            return True
        except Exception:  # noqa: BLE001 — discovery only
            return False

    def list_tools(self) -> list[dict[str, Any]]:
        public = []
        for tool in self._registry.list():
            entry = tool.to_dict()
            entry.pop("handler", None)         # redacted: no executable reference
            public.append(entry)
        return public

    def invoke_for_agent(self, tool_id: str, request: Mapping[str, Any], *,
                         actor: str, session_id: str = "", mission_id: str = "",
                         verify_spec: tuple[dict[str, Any], ...] = (),
                         resource: str = "workspace",
                         requested_operation: str = "",
                         grant: Optional[AuthorizationGrant] = None,
                         granted_capabilities: Optional[tuple[str, ...]] = None):
        """The binding entry both Agent and swarm agents share: builds the
        InvocationContext (grants default to the operator binding; granted
        capabilities default to the union of registered tools) and crosses."""
        if granted_capabilities is None:
            granted_capabilities = tuple(sorted({
                cap for entry in self.list_tools()
                for cap in (entry.get("capabilities") or [])
            }))
        if grant is None:
            # the operator binding supplies consent; the grant follows the
            # REQUESTER so actor mismatches cannot silently occur
            grant = AuthorizationGrant(actor=actor, tool_id=tool_id,
                                       policy_version=self._policy.version)
        context = InvocationContext(
            actor=actor, session_id=session_id, mission_id=mission_id,
            granted_capabilities=tuple(granted_capabilities), authorization=grant,
            requested_operation=requested_operation or tool_id,
            resource=resource, verify_spec=tuple(verify_spec),
        )
        return self.invoke(tool_id, request, context)

    @property
    def policy_identity(self) -> dict[str, str]:
        return {"policy_id": self._policy.policy_id, "policy_version": self._policy.version}

    @property
    def policy_stats(self) -> dict[str, int]:
        return dict(self._policy_stats)

    # -- the single path ---------------------------------------------------- #
    def invoke(self, tool_id: str, request: Mapping[str, Any],
               context: InvocationContext) -> GatewayOutcome:
        problems = context.validate_problems()
        if problems:
            return self._record_refusal(tool_id, context, "CONTEXT_INVALID",
                                        f"malformed context: {'; '.join(problems[:3])}")
        try:
            request = dict(request)
        except Exception:  # noqa: BLE001
            return self._record_refusal(tool_id, context, "CONTEXT_INVALID",
                                        "request must be a mapping")
        if _digest(request) == "unserializable":
            return self._record_refusal(tool_id, context, "CONTEXT_INVALID",
                                        "request is not serializable")

        # 1) registry discovery (metadata for observation + evidence)
        try:
            descriptor = self._registry.get(tool_id)
        except Exception:  # noqa: BLE001 — unknown tool
            return self._record_refusal(tool_id, context, InvocationStatus.NOT_FOUND.value,
                                        f"{tool_id}: no such tool")

        # 2) observation BEFORE — only tools that declare filesystem effects
        filesystem_tool = "filesystem" in descriptor.side_effects
        before = None
        if filesystem_tool:
            try:
                before = WorkspaceObserver(self._workspace).snapshot()
            except Exception as exc:  # noqa: BLE001 — observation failure is fail-closed
                return self._record_refusal(tool_id, context, "OBSERVATION_ERROR",
                                            f"pre-observation failed: {exc.__class__.__name__}")

        # 3) the gated invocation — T5 pipeline with T6 governance injected
        grant = context.authorization
        outcome5 = _registry_invoke(
            self._registry, tool_id, request,
            granted_capabilities=frozenset(context.granted_capabilities),
            capability_resolver=t5_capability_resolver(self._catalog),
            policy=t5_policy_adapter(self._policy, boundary=self._boundary,
                                     stats=self._policy_stats,
                                     resource_of=lambda args, _ctx=context: _ctx.resource,
                                     operation_of=lambda args, _ctx=context: _ctx.requested_operation),
            authorizer=t5_authorizer_adapter(
                self._authorizer, actor=context.actor,
                policy_version_of=lambda d, a: self._policy.version,
                grant_for=lambda d, a: grant),
            evidence=self.evidence,
        )
        handler_called = outcome5.executed
        executed_ok = outcome5.status is InvocationStatus.EXECUTED

        # 4) observation AFTER + delta
        observation_payload: Optional[dict[str, Any]] = None
        if handler_called and filesystem_tool:
            try:
                after = WorkspaceObserver(self._workspace).snapshot()
                delta: FilesystemDelta = WorkspaceObserver(self._workspace).delta(before, after)  # type: ignore[arg-type]
                observation_payload = delta.to_dict()
            except Exception as exc:  # noqa: BLE001 — side effects happened; report honestly
                observation_payload = {"error": f"post-observation failed: {exc.__class__.__name__}"}

        # 5) deterministic verification (T3) — only for executed calls that ask for it
        verification_payload: Optional[dict[str, Any]] = None
        verification_failed = False
        if handler_called and context.verify_spec:
            try:
                result_map = outcome5.result if isinstance(outcome5.result, dict) else {}
                report = DeterministicVerifier(self._workspace).verify(
                    list(context.verify_spec),
                    shell_exit_code=result_map.get("exit_code"),
                    shell_timed_out=bool(result_map.get("timed_out", False)),
                    fs_delta=observation_payload,
                )
                verification_payload = report.to_dict()
                verification_failed = verification_payload.get("verdict") == "FAIL"
            except Exception as exc:  # noqa: BLE001 — a verifier crash is INCONCLUSIVE, never PASS
                verification_payload = {"verdict": "INCONCLUSIVE",
                                        "error": f"{exc.__class__.__name__}: {str(exc)[:150]}"}

        # 6) checkpoint (T4) — one provenance record per invocation
        checkpoint_payload: Optional[dict[str, Any]] = None
        if handler_called:
            checkpoint_payload = self._checkpoint_invocation(tool_id, context, descriptor,
                                                             outcome5, verification_payload,
                                                             observation_payload)

        # 7) honest outcome + unified evidence record (refusals included)
        ok = executed_ok and not verification_failed
        decision = "ALLOW" if executed_ok else "DENY"
        return self._record(tool_id, descriptor, context, outcome5, ok=ok,
                            decision=decision, executed=handler_called,
                            execution_status=outcome5.status.value,
                            reason=outcome5.reason,
                            invocation_id=(outcome5.evidence_event or {}).get("invocation_id", ""),
                            observation=observation_payload,
                            verification=verification_payload,
                            checkpoint=checkpoint_payload,
                            result=None if not handler_called else outcome5.result,
                            duration_ms=outcome5.duration_ms)

    # -- internals ---------------------------------------------------------- #
    def _checkpoint_invocation(self, tool_id: str, context: InvocationContext,
                               descriptor: ToolDescriptor, outcome5, verification_payload,
                               observation_payload) -> Optional[dict[str, Any]]:
        try:
            self._mission_seq += 1
            mission = f"{context.mission_id or 'gw'}:{tool_id}:{self._mission_seq}"
            manager = self._recovery.get(mission)
            if manager is None:
                manager = RecoveryManager(self._checkpoints)
                manager.register(mission)
                self._recovery[mission] = manager
            attempts = (outcome5.evidence_event or {}).get("invocation_id", "")
            cp = manager.checkpoint(
                mission, step_id=tool_id,
                observation_fingerprint=(observation_payload or {}).get("after_root_hash", ""),
                evidence_head=(outcome5.evidence_event or {}).get("hash", ""),
                plan_state={"requested_operation": context.requested_operation or tool_id,
                            "resource": context.resource},
                authorization_state={"granted": context.authorization is not None,
                                     "expires_at": str(context.authorization.expires_at or "")
                                     if context.authorization else ""},
                execution_state={"attempts": [{"step_id": tool_id, "action_hash": attempts,
                                               "status": outcome5.status.value.lower(),
                                               "verified": (verification_payload or {}).get("verdict") == "PASS"}]},
            )
            verdict = (verification_payload or {}).get("verdict")
            if outcome5.status is not InvocationStatus.EXECUTED or verdict == "FAIL":
                manager.fail(mission, f"{outcome5.status.value}/verdict={verdict}")
            elif verdict == "PASS":
                manager.complete(mission, "gateway invocation verified")
            # INCONCLUSIVE / no verify spec ⇒ stays CHECKPOINTED — diagnosable
            return {"checkpoint_id": cp.checkpoint_id, "state": manager.state(mission).value,
                    "mission": mission}
        except Exception as exc:  # noqa: BLE001 — checkpoint problems are recorded, never fatal
            return {"error": f"{exc.__class__.__name__}: {str(exc)[:120]}"}

    def _record_refusal(self, tool_id: str, context: InvocationContext,
                        status: str, reason: str) -> GatewayOutcome:
        outcome5 = None
        return self._record(tool_id, None, context, outcome5, ok=False,
                            decision="DENY", executed=False, execution_status=status,
                            reason=reason, invocation_id="", observation=None,
                            verification=None, checkpoint=None, result=None, duration_ms=0)

    def _record(self, tool_id: str, descriptor: Optional[ToolDescriptor],
                context: InvocationContext, outcome5, *, ok: bool, decision: str,
                executed: bool, execution_status: str, reason: str, invocation_id: str,
                observation: Optional[dict[str, Any]], verification: Optional[dict[str, Any]],
                checkpoint: Optional[dict[str, Any]], result: Any,
                duration_ms: int) -> GatewayOutcome:
        grant = context.authorization
        grant_digest = _digest({"actor": grant.actor, "tool_id": grant.tool_id,
                                "policy_version": grant.policy_version}) if grant else ""
        event = {
            "stage": "gateway",
            "tool_id": tool_id,
            "tool_version": getattr(descriptor, "version", ""),
            "actor": context.actor,
            "session_id": context.session_id,
            "capabilities": sorted(getattr(descriptor, "capabilities", ()) or ()),
            "policy_id": self._policy.policy_id,
            "policy_version": self._policy.version,
            "authorization_id": grant_digest[:21] if grant_digest else "no-grant",
            "decision": decision,
            "execution_status": execution_status,
            "handler_called": executed,
            "reason": str(reason)[:300],
            "observation": self._observation_digest(observation),
            "verification": {"verdict": (verification or {}).get("verdict", ""),
                             "passed": (verification or {}).get("summary", {}).get("passed", 0)}
                            if verification is not None else None,
            "checkpoint": ({"checkpoint_id": (checkpoint or {}).get("checkpoint_id", ""),
                            "state": (checkpoint or {}).get("state", "")}
                           if checkpoint else None),
            "duration_ms": duration_ms,
        }
        event["request_digest"] = _digest({"tool_id": tool_id,
                                           "resource": context.resource,
                                           "operation": context.requested_operation})
        event.pop("_placeholder", None)
        stored = self.evidence.append(event)
        return GatewayOutcome(tool_id=tool_id, ok=ok, handler_called=executed,
                              decision=decision, execution_status=execution_status,
                              reason=reason, result=result, invocation_id=invocation_id,
                              observation=observation, verification=verification,
                              checkpoint=checkpoint, record=stored, duration_ms=duration_ms)

    @staticmethod
    def _observation_digest(observation: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if observation is None:
            return None
        if observation.get("error"):
            return {"error": observation["error"]}
        return {"before_root_hash": str(observation.get("before_root_hash", ""))[:19],
                "after_root_hash": str(observation.get("after_root_hash", ""))[:19],
                "delta_changes": len(observation.get("changes") or []),
                "summary": observation.get("summary", {})}
