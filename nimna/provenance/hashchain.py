"""Small deterministic primitives shared by audit evidence and manifests."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def evidence_hash(
    *,
    session_id: str | None,
    run_id: str | None,
    event: str,
    payload: Any,
    created_at: str,
    previous_hash: str | None,
) -> str:
    """Hash the stable audit fields, excluding the hash itself."""

    return sha256_hex(
        {
            "session_id": session_id,
            "run_id": run_id,
            "event": event,
            "payload": payload,
            "created_at": created_at,
            "previous_hash": previous_hash,
        }
    )


__all__ = ["canonical_json", "evidence_hash", "sha256_hex"]
