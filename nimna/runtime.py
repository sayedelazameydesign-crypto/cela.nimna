"""AgentRuntime: one object owns the truth; interfaces are thin adapters.

The Runtime does NOT re-implement the agent — it delegates execution to the
existing :class:`~nimna.core.agent.Agent` (single construction via
:func:`~nimna.bootstrap.build_agent`, single store, single journal). What the
Runtime adds is lifecycle (``boot``/``shutdown``), a single broadcast stream
(:mod:`nimna.events`), session-scoped approvals, and a deterministic per-run
audit digest for cross-interface parity checks.

Lifecycle — five calls only; nothing outside them mutates runtime state::

    rt = AgentRuntime.boot(settings)
    out = rt.run(RunRequest("analyse sales.csv", session_id="s1"))  # or rt.run("...")
    out = rt.approve(approval_id, "s1", approved=True)   # KeyError / PermissionError
    out = rt.resume(run_id, approved=False)              # explicit decision, no silent default
    rt.shutdown()

Deviations from the sketch this implements, on purpose:

* ``resume`` requires an explicit ``approved`` flag. A confirm-risk tool must
  never be resumed by a default-argument approval.
* Bus events are post-run broadcasts (``run_finished``/``failed``/``suspended``
  + ``skills_used``/``tools_called``). ``Agent`` has no callback / generator /
  stream points and providers are sync request/response, so true live
  streaming is a follow-up AFTER wiring hooks into agent.py — not now. Do
  not read ``RuntimeEvent`` as fulfilling that promise yet.
* ``CostGuard`` is Agent-held (one budget per ``Agent``). Budget integrity
  therefore assumes one Runtime per process — true of every current
  entrypoint (CLI invocation, API server, scripts). To share a budget,
  share the ``Agent`` explicitly (``AgentRuntime(settings, agent=...)``).
* ``approval_id`` is the run id (``PendingApproval.approval_id == run_id``),
  same convention as ``POST /api/approvals/{id}``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from .bootstrap import build_agent
from .config import Settings
from .core.agent import Agent
from .core.approval import ApprovalPolicy
from .core.state import AgentResult, RunStatus
from .events import (
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    RUNTIME_SHUTDOWN,
    RUN_FAILED,
    RUN_FINISHED,
    RUN_SUSPENDED,
    SKILLS_USED,
    TOOLS_CALLED,
    EventBus,
    RuntimeEvent,
)

log = logging.getLogger(__name__)

# Audit payload keys that legitimately differ between two identical runs
# (wall-clock measurements, tamper-evidence chain links). Excluded from the
# parity digest; the hashchain itself still guards tampering.
VOLATILE_AUDIT_KEYS = frozenset({"_evidence", "elapsed_ms"})

# Cumulative runtime-economy counters (grow with every call the runtime ever
# makes, so two identical runs at different positions in the runtime's life
# legitimately disagree). Excluded from the parity digest, which covers
# per-run behaviour, not the runtime's lifetime odometer.
CUMULATIVE_COST_KEYS = frozenset(
    {"request_count", "blocked_count", "spent_usd", "reserved_usd", "remaining_usd"}
)


def _stable_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        clean = {
            k: _stable_value(k, v)
            for k, v in value.items()
            if k not in VOLATILE_AUDIT_KEYS
        }
        if key == "cost_guard":
            clean = {k: v for k, v in clean.items() if k not in CUMULATIVE_COST_KEYS}
        return clean
    if isinstance(value, list):
        return [_stable_value(key, v) for v in value]
    return value


@dataclass
class RunRequest:
    message: str
    session_id: Optional[str] = None
    swarm: Optional[bool] = None  # per-call override; None = settings default


@dataclass
class RunResult:
    """An agent outcome plus its deterministic audit digest."""

    result: AgentResult
    audit_digest: str

    @property
    def run_id(self) -> str:
        return self.result.run_id

    @property
    def status(self) -> RunStatus:
        return self.result.status


def _stable_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project audit rows onto fields that are stable across identical runs."""
    projected = []
    for row in rows:
        payload = row.get("payload") or {}
        clean = {k: _stable_value(k, v) for k, v in payload.items() if k not in VOLATILE_AUDIT_KEYS}
        projected.append({"event": row.get("event"), "payload": clean})
    return projected


class AgentRuntime:
    """Façade over one Agent + one store + one journal + one bus."""

    def __init__(
        self,
        settings: Settings,
        *,
        agent: Optional[Agent] = None,
        approval_policy: Optional[ApprovalPolicy] = None,
        bus: Optional[EventBus] = None,
    ) -> None:
        self.settings = settings
        self.bus = bus if bus is not None else EventBus()
        self.agent = agent if agent is not None else build_agent(
            settings, approval_policy=approval_policy
        )
        self._shutdown = False

    # -- single-truth accessors (owned by reference, never copied) ---------
    @property
    def provider(self):  # governed provider (cost guard at the boundary)
        return self.agent.provider

    @property
    def skills(self):
        return self.agent.skills

    @property
    def tools(self):
        return self.agent.tools

    @property
    def memory(self):
        return self.agent.memory

    @property
    def cost_guard(self):
        return self.agent.cost_guard

    @property
    def policy(self):
        return self.agent.policy

    @property
    def evidence(self):
        return self.agent.evidence

    def pending_approvals(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        """The one pending list (store-backed, expiry-purged)."""
        return self.agent.pending_approvals(session_id)

    def audit(
        self,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.memory.get_audit(session_id, run_id, limit)

    def verify_audit(
        self,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.memory.verify_audit_chain(session_id, run_id)

    # -- lifecycle ----------------------------------------------------------
    @classmethod
    def boot(cls, settings: Optional[Settings] = None, **kwargs: Any) -> "AgentRuntime":
        return cls(settings if settings is not None else Settings.from_env(), **kwargs)

    def run(self, request: RunRequest | str) -> RunResult:
        self._ensure_live()
        req = request if isinstance(request, RunRequest) else RunRequest(message=request)
        res = self.agent.run(req.message, session_id=req.session_id, swarm=req.swarm)
        return self._wrap(res, extra_events=())

    def approve(
        self,
        approval_id: str,
        session_id: str,
        *,
        approved: bool = True,
        always: bool = False,
    ) -> RunResult:
        """Session-scoped approval (mirrors ``POST /api/approvals/{id}``)."""
        self._ensure_live()
        pending = self.memory.get_pending(approval_id)
        if pending is None:
            raise KeyError(f"no pending approval for id '{approval_id}'")
        if pending.get("session_id") != session_id:
            raise PermissionError("pending belongs to a different session")
        out = self.resume(approval_id, approved, always=always)
        self._emit(APPROVAL_RESOLVED, out.result, {"approved": approved, "always": always})
        return out

    def resume(self, run_id: str, approved: bool, *, always: bool = False) -> RunResult:
        """Continue a suspended run with an explicit decision (no default)."""
        self._ensure_live()
        res = self.agent.resume(run_id, approved, always=always)
        return self._wrap(res, extra_events=())

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._emit(RUNTIME_SHUTDOWN, None, {})
        try:
            self.memory.close()
        except Exception:  # pragma: no cover - shutdown must not raise
            log.exception("memory close failed during shutdown")
        finally:
            self._shutdown = True

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    # -- internals ----------------------------------------------------------
    def _ensure_live(self) -> None:
        if self._shutdown:
            raise RuntimeError("runtime is shut down")

    def _emit(
        self, event_type: str, res: Optional[AgentResult], payload: dict[str, Any]
    ) -> None:
        self.bus.publish(
            RuntimeEvent(
                type=event_type,
                session_id=res.session_id if res else "",
                run_id=res.run_id if res else "",
                payload=payload,
            )
        )

    def _wrap(self, res: AgentResult, *, extra_events: tuple[str, ...]) -> RunResult:
        if res.status == RunStatus.DONE:
            self._emit(RUN_FINISHED, res, {"steps": res.steps})
        elif res.status == RunStatus.ERROR:
            self._emit(RUN_FAILED, res, {"error": res.error or ""})
        elif res.status == RunStatus.AWAITING_APPROVAL and res.pending is not None:
            self._emit(RUN_SUSPENDED, res, {"tool": res.pending.tool_name})
            self._emit(
                APPROVAL_REQUESTED,
                res,
                {
                    "approval_id": res.pending.approval_id,
                    "tool": res.pending.tool_name,
                    "summary": res.pending.summary,
                },
            )
        for evt in extra_events:
            self._emit(evt, res, {})
        self._emit(SKILLS_USED, res, {"skills": list(res.skills_used)})
        self._emit(
            TOOLS_CALLED,
            res,
            {"tools": [{"name": c.name, "ok": c.ok} for c in res.tool_calls]},
        )
        return RunResult(result=res, audit_digest=self.audit_digest(res.run_id))

    def audit_digest(self, run_id: str) -> str:
        """Deterministic sha256 over the run's stable audit projection.

        Two identical runs (same request, same provider script) produce the
        same digest even in different sessions — the cross-interface parity
        check. Volatile keys (timestamps, chain links, timings) are excluded.
        """
        rows = self.memory.get_audit(run_id=run_id)
        canonical = json.dumps(
            _stable_projection(rows), sort_keys=True, ensure_ascii=False, default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
