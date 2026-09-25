"""SQLite-backed memory.

* ``sessions`` / ``messages``  – short-term conversational memory
* ``memories``                 – long-term notes & user preferences
* ``audit_log``                – every skill loaded / tool call / approval + SHA-256 evidence links
* ``pending_runs``             – suspended runs awaiting user approval
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PENDING_TTL_SECONDS = 300
MAX_PENDING_PER_SESSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    title TEXT,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'note',
    content TEXT NOT NULL,
    tags TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    run_id TEXT,
    event TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id, id);
CREATE TABLE IF NOT EXISTS pending_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    decision TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_runs(session_id, resolved_at, created_at);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decision TEXT CHECK (decision IN ('approved','rejected','expired')),
    result TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


class MemoryStore:
    def __init__(self, db_path: Path | str = ":memory:"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            try:
                Path(self.db_path).parent.chmod(0o700)
            except Exception:
                pass
        # use WAL for better concurrency and to survive restarts with pending runs
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            # pragmas for durability + permissions
            try:
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA foreign_keys=ON;")
            except Exception:
                pass
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            if self.db_path != ":memory:":
                try:
                    Path(self.db_path).chmod(0o600)
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sessions & messages ---------------------------------------------
    def ensure_session(self, session_id: str, title: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions(id, created_at, title, meta) VALUES (?,?,?,?)",
                (session_id, _now(), title, None),
            )
            self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.created_at, s.title, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS messages "
                "FROM sessions s ORDER BY s.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_message(self, session_id: str, role: str, content: str,
                    meta: Optional[dict[str, Any]] = None) -> int:
        self.ensure_session(session_id)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
                (session_id, role, content, _dumps(meta) if meta else None, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_messages(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, meta, created_at FROM messages WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for item in result:
            item["meta"] = _loads(item.get("meta"), None)
        return result

    # -- long-term memory ------------------------------------------------
    def save_memory(self, content: str, kind: str = "note", tags: Optional[list[str]] = None,
                    session_id: Optional[str] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories(kind, content, tags, session_id, created_at) VALUES (?,?,?,?,?)",
                (kind, content, ",".join(tags or []), session_id, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_memories(self, kind: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT * FROM memories WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._memory_row(row) for row in rows]

    def search_memories(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Keyword search (LIKE per token, ranked by matches). Swap for FTS/vectors if needed."""
        tokens = [tok for tok in query.lower().split() if len(tok) > 1][:8]
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories ORDER BY id DESC LIMIT 2000"
            ).fetchall()
        scored = []
        for row in rows:
            haystack = f"{row['content']} {row['tags'] or ''}".lower()
            score = sum(1 for tok in tokens if tok in haystack)
            if score or not tokens:
                scored.append((score, row))
        scored.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
        return [self._memory_row(row) for _, row in scored[:limit]]

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["tags"] = [t for t in (item.get("tags") or "").split(",") if t]
        return item

    # -- audit -----------------------------------------------------------
    def log(self, session_id: Optional[str], run_id: Optional[str], event: str,
            payload: Optional[dict[str, Any]] = None) -> None:
        # Never persist raw secrets – redact before storage.  Each entry also
        # receives a SHA-256 link to the previous audit entry.  This is an
        # evidence journal: it detects tampering, while not pretending to be a
        # signed non-repudiation system.
        if payload is not None:
            try:
                from ..tools.base import redact_payload  # local import to avoid cycle
                payload = redact_payload(payload)
            except Exception:
                pass
        clean_payload = dict(payload or {})
        # Callers cannot smuggle a replacement hash into the chain.
        clean_payload.pop("_evidence", None)
        created_at = _now()
        from ..provenance.hashchain import evidence_hash
        with self._lock:
            previous_hash: Optional[str] = None
            row = self._conn.execute("SELECT payload FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            if row is not None:
                previous = _loads(row["payload"], {}) or {}
                previous_hash = ((previous.get("_evidence") or {}).get("hash")) if isinstance(previous, dict) else None
            digest = evidence_hash(
                session_id=session_id,
                run_id=run_id,
                event=event,
                payload=clean_payload,
                created_at=created_at,
                previous_hash=previous_hash,
            )
            stored_payload = {
                **clean_payload,
                "_evidence": {
                    "algorithm": "sha256",
                    "hash": digest,
                    "previous_hash": previous_hash,
                },
            }
            self._conn.execute(
                "INSERT INTO audit_log(session_id, run_id, event, payload, created_at) VALUES (?,?,?,?,?)",
                (session_id, run_id, event, _dumps(stored_payload), created_at),
            )
            self._conn.commit()

    def get_audit(self, session_id: Optional[str] = None, run_id: Optional[str] = None,
                  limit: int = 200) -> list[dict[str, Any]]:
        clauses, params = [], []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for item in result:
            item["payload"] = _loads(item.get("payload"), None)
        return result

    def verify_audit_chain(self, session_id: Optional[str] = None, run_id: Optional[str] = None,
                           limit: int = 1000) -> dict[str, Any]:
        """Verify evidence hashes for a run/session without exposing secrets.

        ``anchored`` is true when a filtered first event points to a hash that
        exists in the complete journal (or is the genesis event).  This lets a
        caller verify a single run while preserving the global append-only link.
        """
        from ..provenance.hashchain import evidence_hash

        clauses, params = [], []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit_log {where} ORDER BY id ASC LIMIT ?", (*params, limit)
            ).fetchall()
            all_rows = self._conn.execute("SELECT payload FROM audit_log ORDER BY id ASC").fetchall()
        all_hashes: set[str] = set()
        for row in all_rows:
            data = _loads(row["payload"], {}) or {}
            if isinstance(data, dict):
                value = (data.get("_evidence") or {}).get("hash")
                if value:
                    all_hashes.add(str(value))
        checked = 0
        invalid: list[int] = []
        first_previous: Optional[str] = None
        for row in rows:
            data = _loads(row["payload"], {}) or {}
            evidence = data.pop("_evidence", None) if isinstance(data, dict) else None
            if not isinstance(evidence, dict):
                invalid.append(int(row["id"]))
                continue
            previous = evidence.get("previous_hash")
            if checked == 0:
                first_previous = previous
            if previous is not None and str(previous) not in all_hashes:
                invalid.append(int(row["id"]))
            expected = evidence_hash(
                session_id=row["session_id"],
                run_id=row["run_id"],
                event=row["event"],
                payload=data,
                created_at=row["created_at"],
                previous_hash=previous,
            )
            checked += 1
            if expected != evidence.get("hash"):
                invalid.append(int(row["id"]))
        anchored = checked == 0 or first_previous is None or str(first_previous) in all_hashes
        return {
            "valid": not invalid,
            "anchored": anchored,
            "checked": checked,
            "invalid_ids": invalid,
            "algorithm": "sha256",
        }

    def _pending_expired(self, created_at: str) -> bool:
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - ts).total_seconds() > PENDING_TTL_SECONDS
        except Exception:
            return False

    def _purge_expired_pending(self) -> None:
        # Expire and clean old pending runs (TTL + max per session)
        try:
            from datetime import datetime, timezone, timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=PENDING_TTL_SECONDS)).isoformat()
            with self._lock:
                # Mark expired as resolved with decision='expired' and also insert into approvals for audit
                rows = self._conn.execute(
                    "SELECT id, session_id FROM pending_runs WHERE resolved_at IS NULL AND created_at < ?", (cutoff,)
                ).fetchall()
                for r in rows:
                    self._conn.execute(
                        "UPDATE pending_runs SET resolved_at = ?, decision = 'expired' WHERE id = ? AND resolved_at IS NULL",
                        (_now(), r["id"]),
                    )
                    # log expiry
                    self._conn.execute(
                        "INSERT OR IGNORE INTO approvals(id, session_id, tool_name, arguments, requested_at, decided_at, decision) VALUES (?,?,?,?,?,?,?)",
                        (r["id"], r["session_id"], "unknown", "{}", r["id"], _now(), "expired"),
                    )
                # Enforce max per session: keep only newest MAX_PENDING_PER_SESSION, expire oldest
                for (sid,) in self._conn.execute("SELECT DISTINCT session_id FROM pending_runs WHERE resolved_at IS NULL").fetchall():
                    cnt = self._conn.execute("SELECT COUNT(*) FROM pending_runs WHERE session_id=? AND resolved_at IS NULL", (sid,)).fetchone()[0]
                    if cnt > MAX_PENDING_PER_SESSION:
                        to_expire = cnt - MAX_PENDING_PER_SESSION
                        old_rows = self._conn.execute(
                            "SELECT id FROM pending_runs WHERE session_id=? AND resolved_at IS NULL ORDER BY created_at ASC LIMIT ?",
                            (sid, to_expire),
                        ).fetchall()
                        for rr in old_rows:
                            self._conn.execute(
                                "UPDATE pending_runs SET resolved_at=?, decision='expired' WHERE id=? AND resolved_at IS NULL",
                                (_now(), rr["id"]),
                            )
                self._conn.commit()
        except Exception:
            pass

    # -- pending approvals -----------------------------------------------
    def save_pending(self, run_id: str, session_id: str, state: dict[str, Any]) -> None:
        self._purge_expired_pending()
        # Enforce max per session before insert
        with self._lock:
            cnt = self._conn.execute(
                "SELECT COUNT(*) FROM pending_runs WHERE session_id=? AND resolved_at IS NULL", (session_id,)
            ).fetchone()[0]
            if cnt >= MAX_PENDING_PER_SESSION:
                raise RuntimeError(f"too many pending approvals for session {session_id} (max {MAX_PENDING_PER_SESSION})")
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_runs(id, session_id, state, created_at, resolved_at, decision) "
                "VALUES (?,?,?,?,NULL,NULL)",
                (run_id, session_id, _dumps(state), _now()),
            )
            # also insert into approvals audit
            try:
                tool_name = (state.get("pending") or {}).get("tool_name") or "unknown"
                args = (state.get("pending") or {}).get("tool_call") or {}
                if isinstance(args, dict):
                    args_j = _dumps(args.get("arguments") or {})
                else:
                    args_j = "{}"
                self._conn.execute(
                    "INSERT OR IGNORE INTO approvals(id, session_id, tool_name, arguments, requested_at) VALUES (?,?,?,?,?)",
                    (run_id, session_id, tool_name, args_j, _now()),
                )
            except Exception:
                pass
            self._conn.commit()

    def get_pending(self, run_id: str) -> Optional[dict[str, Any]]:
        self._purge_expired_pending()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_runs WHERE id = ? AND resolved_at IS NULL", (run_id,)
            ).fetchone()
        if row is None:
            return None
        # check TTL explicitly (in case purge missed due to clock)
        if self._pending_expired(row["created_at"]):
            # expire on read
            with self._lock:
                self._conn.execute(
                    "UPDATE pending_runs SET resolved_at=?, decision='expired' WHERE id=? AND resolved_at IS NULL",
                    (row["created_at"], run_id),
                )
                self._conn.commit()
            return None
        return _loads(row["state"], None)

    def get_pending_with_session(self, run_id: str, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Session-scoped get — prevents one session approving another's pending."""
        data = self.get_pending(run_id)
        if data is None:
            return None
        if session_id is not None and data.get("session_id") != session_id:
            return None
        return data

    def resolve_pending(self, run_id: str, decision: str, session_id: Optional[str] = None) -> None:
        self._purge_expired_pending()
        with self._lock:
            # Session scoping: verify owner
            if session_id is not None:
                row = self._conn.execute("SELECT session_id, resolved_at FROM pending_runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(f"no pending approval for run '{run_id}'")
                if row["session_id"] != session_id:
                    raise PermissionError(f"pending '{run_id}' belongs to different session")
                if row["resolved_at"] is not None:
                    raise KeyError(f"run '{run_id}' already resolved ({row['resolved_at']})")
            # Atomic: only transition from NULL -> timestamp, prevents duplicate resume (one-shot)
            cur = self._conn.execute(
                "UPDATE pending_runs SET resolved_at = ?, decision = ? WHERE id = ? AND resolved_at IS NULL",
                (_now(), decision, run_id),
            )
            # also update approvals audit
            try:
                self._conn.execute(
                    "UPDATE approvals SET decided_at=?, decision=?, result=? WHERE id=?",
                    (_now(), decision, None, run_id),
                )
            except Exception:
                pass
            self._conn.commit()
            if cur.rowcount == 0:
                row = self._conn.execute("SELECT resolved_at FROM pending_runs WHERE id = ?", (run_id,)).fetchone()
                if row is not None and row["resolved_at"] is not None:
                    raise KeyError(f"run '{run_id}' already resolved ({row['resolved_at']})")
                raise KeyError(f"no pending approval for run '{run_id}'")

    def list_pending(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        self._purge_expired_pending()
        with self._lock:
            if session_id:
                rows = self._conn.execute(
                    "SELECT id, session_id, created_at FROM pending_runs "
                    "WHERE resolved_at IS NULL AND session_id = ? ORDER BY created_at",
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, session_id, created_at FROM pending_runs WHERE resolved_at IS NULL "
                    "ORDER BY created_at"
                ).fetchall()
        return [dict(row) for row in rows]
