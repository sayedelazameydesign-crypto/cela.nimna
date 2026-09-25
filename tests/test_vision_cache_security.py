"""Vision-cache security: no pickle, no credential leak (offline, no Redis needed).

Regression tests for the 2026-09-25 hardening:
  * Redis payloads are JSON — ``pickle.loads`` on network bytes is RCE and is banned.
  * ``stats()["redis_url"]`` is served on the *unauthenticated* ``/api/health``
    endpoint, so it must never echo userinfo (``redis://:password@host``).
"""
from __future__ import annotations

import json
import pathlib

import pytest

from nimna.vision.cache import VisionCache, redact_url, reset_vision_cache


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_vision_cache()
    yield
    reset_vision_cache()


class _FakeRedis:
    """Minimal dict-backed stand-in for redis.Redis (no server needed)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def ping(self):
        return True

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else bytes(value)

    def scan_iter(self, match=None):
        return iter(list(self.store))

    def delete(self, key):
        self.store.pop(key, None)


def _enabled_cache(url: str = "redis://localhost:6379/0") -> VisionCache:
    cache = VisionCache.__new__(VisionCache)
    cache.ttl = 600
    cache.redis_url = url
    cache._redis = _FakeRedis()  # noqa: SLF001 — deliberate test seam
    cache._enabled = True
    assert cache.enabled
    return cache


def test_pickle_is_absent_from_the_cache_module():
    source = (pathlib.Path(__file__).resolve().parent.parent / "nimna" / "vision" / "cache.py")
    text = source.read_text(encoding="utf-8")
    for banned in ("import pickle", "pickle.loads", "pickle.dumps", "pickle.load"):
        assert banned not in text, f"{banned} is banned in the vision cache (RCE vector)"


def test_redis_payload_is_json_not_pickle():
    cache = _enabled_cache()
    value = {"b64": "aGVsbG8=", "mime": "image/png", "caption": "[Screenshot: x]", "hash": "abc123"}
    cache.set(b"fake-png-bytes", 640, 400, value)
    stored = next(iter(cache._redis.store.values()))  # noqa: SLF001
    assert stored[:1] == b"{", "Redis payload must be a JSON document"
    assert json.loads(stored.decode("utf-8")) == value


def test_json_round_trip_through_fake_redis():
    cache = _enabled_cache()
    value = {"b64": "eA==", "mime": "image/png", "caption": "c", "hash": "h"}
    payload = b"fake-png-bytes"
    cache.set(payload, 100, 200, value)
    assert cache.get(payload, 100, 200) == value


def test_legacy_or_hostile_bytes_are_a_safe_miss_not_rce(monkeypatch):
    cache = _enabled_cache()
    payload = b"fake-png-bytes"
    # legacy pickle bytes (pre-hardening releases) and hostile pickle payloads
    # must both degrade to a cache MISS — never executed, never raised.
    import pickle

    legacy_blob = pickle.dumps({"b64": "old"})
    # protocol-0 payload: os.system('echo nimna-pickle-probe') if unpickled.
    hostile_blob = b"cos\nsystem\n(S'echo nimna-pickle-probe'\ntR."
    executed = []
    monkeypatch.setattr("os.system", lambda *a, **k: executed.append(a) or 0)
    for blob in (legacy_blob, hostile_blob):
        cache._redis.store.clear()  # noqa: SLF001
        from nimna.vision import cache as cache_mod

        cache._redis.store[cache_mod._key(payload, 0, 0)] = blob  # noqa: SLF001
        assert cache.get(payload, 0, 0) is None
    assert executed == [], "a hostile cache payload was executed!"


def test_non_serialisable_values_skip_redis_but_stay_in_memory():
    cache = _enabled_cache()
    payload = b"fake-png-bytes"
    cache.set(payload, 0, 0, {"ok": object()})  # not JSON-serialisable
    assert cache._redis.store == {}, "unserialisable values must not reach Redis"  # noqa: SLF001
    assert cache.get(payload, 0, 0) == {"ok": cache.get(payload, 0, 0)["ok"]}  # memory fallback


@pytest.mark.parametrize(
    ("url", "forbidden", "expected"),
    [
        ("redis://:s3cret-pw@redis:6379/0", "s3cret-pw", "redis://redis:6379/0"),
        ("redis://user:pw@localhost:6379/0", "pw", "redis://localhost:6379/0"),
        ("rediss://:tok@cache.example.com:6380/1", "tok", "rediss://cache.example.com:6380/1"),
    ],
)
def test_stats_redacts_credentials(url, forbidden, expected):
    assert redact_url(url) == expected
    assert forbidden not in redact_url(url)
    cache = _enabled_cache(url)
    assert cache.stats()["redis_url"] == expected
    assert forbidden not in json.dumps(cache.stats())


def test_stats_without_redis_keeps_the_memory_marker():
    cache = VisionCache(redis_url="")
    assert cache.enabled is False
    assert cache.stats()["redis_url"] == "fallback:memory"


def test_health_endpoint_never_echoes_redis_credentials(monkeypatch):
    """End-to-end: /api/health is unauthenticated — assert no password leaks."""
    import secrets

    from fastapi.testclient import TestClient

    password = "health-leak-probe-" + secrets.token_hex(4)
    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("NIMNA_ENV", "development")
    monkeypatch.setenv("REDIS_URL", f"redis://:{password}@localhost:6379/0")
    monkeypatch.setenv("VISION_CACHE_TTL", "600")
    reset_vision_cache()
    from nimna.api.app import create_app

    client = TestClient(create_app())
    body = client.get("/api/health").json()
    assert password not in json.dumps(body)
