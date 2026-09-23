"""SQLite-backed memory.

* ``sessions`` / ``messages``  – short-term conversational memory
* ``memories``                 – long-term notes & user preferences
* ``audit_log``                – every skill loaded / tool call / approval
* ``pending_runs``             – suspended runs awaiting user approval
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
        # never persist raw secrets – redact before storage
        if payload is not None:
            try:
                from ..tools.base import redact_payload  # local import to avoid cycle
                payload = redact_payload(payload)
            except Exception:
                pass
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log(session_id, run_id, event, payload, created_at) VALUES (?,?,?,?,?)",
                (session_id, run_id, event, _dumps(payload) if payload is not None else None, _now()),
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

    # -- pending approvals -----------------------------------------------
    def save_pending(self, run_id: str, session_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_runs(id, session_id, state, created_at, resolved_at, decision) "
                "VALUES (?,?,?,?,NULL,NULL)",
                (run_id, session_id, _dumps(state), _now()),
            )
            self._conn.commit()

    def get_pending(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_runs WHERE id = ? AND resolved_at IS NULL", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return _loads(row["state"], None)

    def resolve_pending(self, run_id: str, decision: str) -> None:
        with self._lock:
            # Atomic: only transition from NULL -> timestamp, prevents duplicate resume
            cur = self._conn.execute(
                "UPDATE pending_runs SET resolved_at = ?, decision = ? WHERE id = ? AND resolved_at IS NULL",
                (_now(), decision, run_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                # check if it exists but already resolved → duplicate resume
                row = self._conn.execute("SELECT resolved_at FROM pending_runs WHERE id = ?", (run_id,)).fetchone()
                if row is not None and row["resolved_at"] is not None:
                    raise KeyError(f"run '{run_id}' already resolved ({row['resolved_at']})")
                raise KeyError(f"no pending approval for run '{run_id}'")

    def list_pending(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
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
