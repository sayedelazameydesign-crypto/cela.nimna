"""Single event stream for every interface (CLI / REST / WS / Web).

``RuntimeEvent`` is the only broadcast type: adapters translate it, they never
invent their own. ``EventBus`` is a tiny in-process pub/sub with synchronous
callbacks — the WebSocket adapter bridges it to sockets, the CLI prints it.
A failing subscriber must never break a run, so ``publish`` isolates errors.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# -- terminal broadcast (emitted post-run; the audit journal stays canonical) --
RUN_FINISHED = "run_finished"
RUN_FAILED = "run_failed"
RUN_SUSPENDED = "run_suspended"
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_RESOLVED = "approval_resolved"
SKILLS_USED = "skills_used"
TOOLS_CALLED = "tools_called"
RUNTIME_SHUTDOWN = "runtime_shutdown"


@dataclass(frozen=True)
class RuntimeEvent:
    """One broadcast unit. ``payload`` is JSON-serialisable adapter material."""

    type: str
    session_id: str = ""
    run_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)


Subscriber = Callable[[RuntimeEvent], None]


class EventBus:
    """Minimal in-process fan-out. Sync by design (no hidden threads)."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Register ``fn``; returns an ``unsubscribe()`` closure."""
        self._subscribers.append(fn)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass

        return _unsubscribe

    def publish(self, event: RuntimeEvent) -> None:
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception:  # pragma: no cover - a broken sink must not break a run
                log.exception("event subscriber failed for %s", event.type)

    def __len__(self) -> int:
        return len(self._subscribers)
