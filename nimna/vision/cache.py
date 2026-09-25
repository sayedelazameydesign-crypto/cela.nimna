"""Vision Gateway Caching Layer — Redis + in-memory fallback.

Caches Gemini Vision analysis results for identical screenshots to cut 80% of calls.
Key = vision:{hash}:{w}x{h} where hash is 16x16 grayscale sha256 (same as agent hash).
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
from typing import Any

# in-memory fallback (LRU-ish)
_FALLBACK: dict[str, tuple[Any, float]] = {}
_FALLBACK_MAX = 256
_STATS = {"hits": 0, "misses": 0, "sets": 0}

def _hash_image(data: bytes, w: int = 0, h: int = 0) -> str:
    try:
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("L").resize((16, 16))
        return hashlib.sha256(img.tobytes()).hexdigest()[:12]
    except Exception:
        return hashlib.sha256(data[:4096]).hexdigest()[:12]


# ── codec (S301, قرار 2026-09-25): JSON موقّع ببادئة إصدار — لا pickle إطلاقًا ──
# التتبع الإمبيريكي (تقرير التدقيق § decision-S301): الموضع الوحيد للكتابة
# (agent.py) يخزّن dict من 4 حقول نصية حصراً → JSON مباشر بلا غلاف قيم.
# أي بايتات بلا بادئة J1 (بما فيها مخلفات pickle قبل الإصلاح) = cache miss —
# لا يُفكّ تسلسلها بشيء، وتموت بـTTL (≤600 ثانية) دون أي migration.
_PREFIX_JSON = b"J1"


def _dumps(value: Any) -> bytes:
    return _PREFIX_JSON + json.dumps(value, ensure_ascii=False).encode("utf-8")


def _loads(data: bytes) -> Any | None:
    """قراءة صارمة: J1+JSON فقط. كل ما عدا ذلك → None (miss لا crash)."""
    if data[:2] == _PREFIX_JSON:
        try:
            return json.loads(data[2:].decode("utf-8"))
        except Exception:
            return None
    return None


def _key(data: bytes, w: int, h: int) -> str:
    h12 = _hash_image(data, w, h)
    return f"vision:{h12}:{w}x{h}"

class VisionCache:
    def __init__(self, redis_url: str | None = None, ttl: int = 600):
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

    def get(self, data: bytes, w: int = 0, h: int = 0) -> Any | None:
        k = _key(data, w, h)
        # try Redis
        if self.enabled:
            try:
                raw = self._redis.get(k)  # type: ignore
                if raw is not None:
                    val = _loads(raw)
                    if val is not None:
                        _STATS["hits"] += 1
                        return val
                    # بادئة غائبة/تالفة (مخلفات pickle قديمة) → miss لا crash
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
                self._redis.setex(k, self.ttl, _dumps(value))  # type: ignore
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
            "redis_url": self.redis_url if self.enabled else "fallback:memory",
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
_singleton: VisionCache | None = None

def get_vision_cache(redis_url: str | None = None, ttl: int | None = None) -> VisionCache:
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
