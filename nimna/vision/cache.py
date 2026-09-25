"""Vision Gateway Caching Layer — Redis + in-memory fallback.

Caches Gemini Vision analysis results for identical screenshots to cut 80% of calls.
Key = vision:{hash}:{w}x{h} where hash is 16x16 grayscale md5 (same as agent hash).
TTL 600s (10m), maxmemory 256mb allkeys-lru on Redis side.

Usage:
    from nimna.vision.cache import get_vision_cache
    cache = get_vision_cache()
    cached = cache.get(image_bytes, width, height)
    if cached: return cached
    result = await gemini.analyze(image_bytes)
    cache.set(image_bytes, width, height, result)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

# in-memory fallback (LRU-ish)
_FALLBACK: dict[str, tuple[Any, float]] = {}
_FALLBACK_MAX = 256
_STATS = {"hits": 0, "misses": 0, "sets": 0}

def _hash_image(data: bytes, w: int = 0, h: int = 0) -> str:
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data)).convert("L").resize((16, 16))
        return hashlib.md5(img.tobytes()).hexdigest()[:12]
    except Exception:
        return hashlib.md5(data[:4096]).hexdigest()[:12]

def _key(data: bytes, w: int, h: int) -> str:
    h12 = _hash_image(data, w, h)
    return f"vision:{h12}:{w}x{h}"


def redact_url(url: str) -> str:
    """Strip userinfo (username/password) from a URL for safe display.

    ``stats()`` is served on the *unauthenticated* ``/api/health`` endpoint,
    so a ``redis://:password@host`` URL must never be echoed back verbatim.
    Unparseable input degrades to a boolean-ish marker instead of leaking.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if not parts.scheme and not parts.netloc:
            return "redis:configured" if url else ""
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        redacted = urlunsplit((parts.scheme or "redis", netloc, parts.path or "", "", ""))
        return redacted if redacted != "redis:" else "redis:configured"
    except Exception:
        return "redis:configured"

class VisionCache:
    def __init__(self, redis_url: Optional[str] = None, ttl: int = 600):
        self.ttl = ttl
        self.redis_url = redis_url or os.getenv("REDIS_URL", "")
        self._redis = None
        self._enabled = bool(self.redis_url)
        if self._enabled:
            try:
                import redis  # type: ignore
                self._redis = redis.from_url(self.redis_url, decode_responses=False, socket_connect_timeout=2, socket_timeout=2)
                self._redis.ping()
            except Exception:
                self._redis = None
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled and self._redis is not None

    def get(self, data: bytes, w: int = 0, h: int = 0) -> Optional[Any]:
        k = _key(data, w, h)
        # try Redis — JSON only.  Never unpickle: cache bytes come from a
        # network service, and deserialising them with pickle would be remote
        # code execution if Redis is ever shared or compromised.
        # Unparseable entries (including values written by older pickle-based
        # releases) are a safe MISS, not an error — the caller simply
        # recomputes and overwrites them.
        if self.enabled:
            try:
                raw = self._redis.get(k)  # type: ignore
                if raw is not None:
                    if isinstance(raw, (bytes, bytearray)):
                        raw = bytes(raw).decode("utf-8")
                    value = json.loads(raw)
                    _STATS["hits"] += 1
                    return value
            except Exception:
                pass
        # fallback memory
        entry = _FALLBACK.get(k)
        if entry is not None:
            val, exp = entry
            if time.time() < exp:
                _STATS["hits"] += 1
                return val
            else:
                _FALLBACK.pop(k, None)
        _STATS["misses"] += 1
        return None

    def set(self, data: bytes, w: int, h: int, value: Any) -> None:
        k = _key(data, w, h)
        _STATS["sets"] += 1
        if self.enabled:
            try:
                # The only producer (agent vision injection) stores a plain
                # dict {"b64","mime","caption","hash"} — JSON-safe by
                # construction.  A non-serialisable value skips Redis and is
                # kept in the in-memory fallback only (never pickled).
                self._redis.setex(k, self.ttl, json.dumps(value))  # type: ignore
                return
            except Exception:
                pass
        # fallback
        if len(_FALLBACK) >= _FALLBACK_MAX:
            # evict oldest
            oldest = min(_FALLBACK.items(), key=lambda x: x[1][1])[0]
            _FALLBACK.pop(oldest, None)
        _FALLBACK[k] = (value, time.time() + self.ttl)

    def stats(self) -> dict[str, Any]:
        total = _STATS["hits"] + _STATS["misses"]
        hit_rate = (_STATS["hits"] / total) if total else 0.0
        return {
            "enabled": self.enabled,
            "redis_url": redact_url(self.redis_url) if self.enabled else "fallback:memory",
            "ttl": self.ttl,
            "hits": _STATS["hits"],
            "misses": _STATS["misses"],
            "sets": _STATS["sets"],
            "hit_rate": round(hit_rate, 3),
            "fallback_size": len(_FALLBACK),
        }

    def clear(self) -> None:
        _FALLBACK.clear()
        _STATS.update({"hits": 0, "misses": 0, "sets": 0})
        if self.enabled:
            try:
                for k in self._redis.scan_iter(match="vision:*"):  # type: ignore
                    self._redis.delete(k)
            except Exception:
                pass

# singleton
_singleton: Optional[VisionCache] = None

def get_vision_cache(redis_url: Optional[str] = None, ttl: Optional[int] = None) -> VisionCache:
    global _singleton
    if _singleton is None:
        ttl = ttl or int(os.getenv("VISION_CACHE_TTL", "600"))
        _singleton = VisionCache(redis_url=redis_url, ttl=ttl)
    return _singleton

def reset_vision_cache() -> None:
    global _singleton, _FALLBACK, _STATS
    if _singleton is not None:
        try:
            _singleton.clear()
        except Exception:
            pass
    _FALLBACK.clear()
    _STATS.update({"hits": 0, "misses": 0, "sets": 0})
    _singleton = None
