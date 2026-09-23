"""Evidence facade over the canonical SQLite audit journal."""
from __future__ import annotations

from typing import Any, Optional


class EvidenceJournal:
    """Record and verify auditable mission events.

    SQLite remains the source of truth; this class intentionally does not make a
    vector index or an in-memory cache authoritative.
    """

    def __init__(self, store: Any):
        self.store = store

    def record(
        self,
        session_id: Optional[str],
        run_id: Optional[str],
        event: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        self.store.log(session_id, run_id, event, payload)

    def for_run(self, run_id: str, *, limit: int = 1000) -> dict[str, Any]:
        events = self.store.get_audit(run_id=run_id, limit=limit)
        return {
            "run_id": run_id,
            "found": bool(events),
            "events": events,
            "verification": self.store.verify_audit_chain(run_id=run_id, limit=limit),
        }

    def for_session(self, session_id: str, *, limit: int = 1000) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "events": self.store.get_audit(session_id=session_id, limit=limit),
            "verification": self.store.verify_audit_chain(session_id=session_id, limit=limit),
        }


__all__ = ["EvidenceJournal"]
