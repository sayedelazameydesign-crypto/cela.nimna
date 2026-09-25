"""Redis-backed chat rate limiting (offline — emulated Redis, no server).

The limiter shares ONE bucket per key across replicas via an atomic Lua
script.  Without a live server we (a) emulate the script's contract with a
fake client to pin the Python side (allow/deny/retry-after/fail-open), and
(b) assert the Lua source itself carries the atomicity primitives so an edit
cannot silently drop them.
"""
from __future__ import annotations

import pytest

from nimna.api.security import RedisSlidingWindowRateLimiter


class _FakeRedis:
    """Emulates the Lua contract: per-key sorted-set sliding window."""

    def __init__(self, clock):
        self.clock = clock
        self.sets: dict[str, dict[str, float]] = {}
        self.scripts: dict[str, str] = {}
        self.fail_with: Exception | None = None
        self.noscript_once = False

    def script_load(self, script: str) -> str:
        digest = f"sha:{len(script)}:{hash(script) & 0xFFFF}"
        self.scripts[digest] = script
        return digest

    def evalsha(self, digest, nkeys, key, now, window, limit, member):
        if self.fail_with is not None:
            raise self.fail_with
        if self.noscript_once:
            self.noscript_once = False
            raiseFake = RuntimeError("NOSCRIPT No matching script. Please use EVAL.")
            raise raiseFake
        assert digest in self.scripts, "evalsha before script_load"
        now, window, limit = float(now), float(window), int(limit)
        bucket = self.sets.setdefault(key, {})
        for member_key in [m for m, score in bucket.items() if score <= now - window]:
            del bucket[member_key]
        if len(bucket) >= limit:
            return [0, min(bucket.values())]
        bucket[member] = now
        return [1, 0]


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _limiter(clock=None, limit=3, window=60.0, fake=None):
    clock = clock or _Clock()
    fake = fake if fake is not None else _FakeRedis(clock)
    limiter = RedisSlidingWindowRateLimiter(fake, limit, window, clock=clock)
    return limiter, fake, clock


def test_allows_under_limit_and_denies_over_with_retry_after():
    limiter, _, clock = _limiter(limit=3)
    assert limiter.hit("id1") == (True, 0.0)
    assert limiter.hit("id1") == (True, 0.0)
    assert limiter.hit("id1") == (True, 0.0)
    allowed, retry_after = limiter.hit("id1")
    assert allowed is False
    assert 0 < retry_after <= 60.0
    # another identity has its own bucket
    assert limiter.hit("id2") == (True, 0.0)


def test_window_slides_and_old_hits_expire():
    limiter, _, clock = _limiter(limit=2, window=10.0)
    assert limiter.hit("id1")[0] is True
    assert limiter.hit("id1")[0] is True
    assert limiter.hit("id1")[0] is False
    clock.now += 11.0
    assert limiter.hit("id1") == (True, 0.0)


def test_same_millisecond_hits_do_not_collapse():
    # member = timestamp:uuid — concurrent hits in the same instant each count.
    limiter, fake, _ = _limiter(limit=2)
    assert limiter.hit("id1")[0] is True
    assert limiter.hit("id1")[0] is True
    assert limiter.hit("id1")[0] is False
    assert len(fake.sets["nimna:ratelimit:chat:id1"]) == 2


def test_redis_error_fails_open_and_counts():
    from nimna.api.telemetry import get_registry, reset_registry

    reset_registry()
    fake = _FakeRedis(_Clock())
    fake.fail_with = ConnectionError("redis down")
    limiter, _, _ = _limiter(limit=1, fake=fake)
    assert limiter.hit("id1") == (True, 0.0)
    assert limiter.hit("id1") == (True, 0.0)
    assert get_registry().get("rate_limiter_errors_total", {"backend": "redis"}) == 2.0


def test_noscript_reloads_and_retries_once():
    limiter, fake, _ = _limiter(limit=5)
    assert limiter.hit("id1")[0] is True  # loads the script
    fake.noscript_once = True  # simulate failover flushing the script cache
    assert limiter.hit("id1")[0] is True


def test_lua_source_keeps_its_atomicity_primitives():
    lua = RedisSlidingWindowRateLimiter._LUA  # noqa: SLF001 — contract pin
    for primitive in ("ZREMRANGEBYSCORE", "ZCARD", "ZADD", "PEXPIRE", "ZRANGE"):
        assert primitive in lua, f"Lua script lost {primitive}"
    assert lua.count("redis.call") >= 4


def test_install_security_prefers_redis_and_falls_back_to_memory(monkeypatch):
    from fastapi import FastAPI

    from nimna.api.security import SecurityConfig, install_security
    from nimna.config import Settings

    settings = Settings.from_env(env_file=None)
    settings.env = "development"
    settings.api_keys = ["test-key-redis-limiter-0123456789ab"]

    config = SecurityConfig.from_settings(settings)

    # redis package present but URL garbage → memory fallback, still boots
    app = FastAPI()
    install_security(app, config, redis_url="not-a-url")
    assert app.state.chat_limiter.backend == "memory"

    # no URL → memory
    app2 = FastAPI()
    install_security(app2, config)
    assert app2.state.chat_limiter.backend == "memory"


def test_rate_limit_429s_are_counted(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nimna.api.telemetry import get_registry, reset_registry

    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("NIMNA_ENV", "development")
    monkeypatch.setenv("NIMNA_API_KEY", "test-key-429-metric-0123456789ab")
    monkeypatch.setenv("NIMNA_CHAT_RATE_LIMIT", "1")
    reset_registry()
    from nimna.api.app import create_app

    client = TestClient(create_app(), headers={"X-Nimna-Key": "test-key-429-metric-0123456789ab"})
    assert client.post("/api/chat", json={"message": "one", "session_id": "s1"}).status_code == 200
    assert client.post("/api/chat", json={"message": "two", "session_id": "s2"}).status_code == 429
    assert get_registry().get("rate_limit_hits_total",
                              {"path": "/api/chat", "backend": "memory"}) == 1.0
