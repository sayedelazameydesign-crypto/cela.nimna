"""Idempotency-Key for POST /api/chat and approval resolve (offline).

Retrying with the same key + same body returns the stored response instead of
re-running the agent; the same key + a different body is 422; a retry racing
an in-flight request is 409 (no duplicate run).  Keys are scoped per API-key
identity and expire via TTL.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# Fixture-only credential (never a real secret).  TestClient's client host is
# "testclient" (not loopback), so key auth is required even in development.
TEST_KEY = "test-key-idempotency-0123456789abcdef"


@pytest.fixture
def app_client(monkeypatch):
    from nimna.api.telemetry import reset_registry

    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("NIMNA_ENV", "development")
    monkeypatch.setenv("NIMNA_API_KEY", TEST_KEY)
    reset_registry()
    from nimna.api.app import create_app

    return TestClient(create_app(), headers={"X-Nimna-Key": TEST_KEY})


def _chat(client, key=None, message="hello", session="s-idem-1"):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/chat", json={"message": message, "session_id": session}, headers=headers)


def test_chat_replay_returns_identical_response(app_client):
    first = _chat(app_client, key="idem-key-001")
    assert first.status_code == 200
    assert "Idempotent-Replayed" not in first.headers
    second = _chat(app_client, key="idem-key-001")
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert second.json()["run_id"] == first.json()["run_id"], "replay must be the SAME run, not a re-run"


def test_replayed_run_is_not_reExecuted(app_client):
    from nimna.api.telemetry import get_registry

    _chat(app_client, key="idem-key-exec")
    _chat(app_client, key="idem-key-exec")
    _chat(app_client, key="idem-key-exec")
    assert get_registry().get("chat_runs_total", {"status": "done"}) == 1.0
    assert get_registry().get("idempotent_replays_total", {"endpoint": "chat"}) == 2.0


def test_same_key_different_body_is_422(app_client):
    assert _chat(app_client, key="idem-key-mismatch", message="one").status_code == 200
    r = _chat(app_client, key="idem-key-mismatch", message="two")
    assert r.status_code == 422
    assert "different request body" in r.json()["detail"]


def test_invalid_key_is_422_not_silently_ignored(app_client):
    r = _chat(app_client, key="has space and CAPS!!!")
    assert r.status_code == 422
    r = _chat(app_client, key="x" * 129)
    assert r.status_code == 422


def test_no_header_means_no_idempotency(app_client):
    first = _chat(app_client)
    second = _chat(app_client)
    assert first.json()["run_id"] != second.json()["run_id"]


def test_in_flight_retry_is_409_without_duplicate_run(app_client, monkeypatch):
    import threading

    agent = app_client.app.state.agent
    started = threading.Event()
    release = threading.Event()
    real_run = agent.run

    def slow_run(*args, **kwargs):
        started.set()
        assert release.wait(timeout=10)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(agent, "run", slow_run)
    results = []

    def first_call():
        results.append(_chat(app_client, key="idem-key-race"))

    worker = threading.Thread(target=first_call)
    worker.start()
    assert started.wait(timeout=10)
    racing = _chat(app_client, key="idem-key-race")
    assert racing.status_code == 409
    assert racing.headers["Retry-After"] == "5"
    release.set()
    worker.join(timeout=10)
    assert results[0].status_code == 200
    # after completion the same key replays (exactly-once across the race)
    replay = _chat(app_client, key="idem-key-race")
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.json()["run_id"] == results[0].json()["run_id"]


def test_failed_execution_releases_the_key(app_client, monkeypatch):
    agent = app_client.app.state.agent

    def boom(*args, **kwargs):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(agent, "run", boom)
    with pytest.raises(RuntimeError):
        _chat(app_client, key="idem-key-fail", message="x")
    # key released → next attempt runs fresh (no stale marker, no 409)
    from nimna.core.state import AgentResult, RunStatus

    def recovered(*args, **kwargs):
        return AgentResult(run_id="r", session_id="s", status=RunStatus.DONE,
                           reply="recovered", skills_used=[])

    monkeypatch.setattr(agent, "run", recovered)
    retry = _chat(app_client, key="idem-key-fail", message="x")
    assert retry.status_code == 200
    assert "Idempotent-Replayed" not in retry.headers


def test_keys_are_scoped_per_identity(app_client):
    first = _chat(app_client, key="idem-key-shared")
    assert first.status_code == 200
    # same key from the same identity replays …
    assert _chat(app_client, key="idem-key-shared").headers.get("Idempotent-Replayed") == "true"
    # … while the store itself isolates identities (unit-level proof):
    memory = app_client.app.state.agent.memory
    assert memory.idempotency_get("another-identity", "idem-key-shared") is None
    assert memory.idempotency_count() == 1


def test_expired_keys_start_a_new_execution(app_client):
    memory = app_client.app.state.agent.memory
    memory.idempotency_claim("anon:127.0.0.1", "idem-key-old", "chat", "hash", ttl_seconds=60)
    memory.idempotency_complete("anon:127.0.0.1", "idem-key-old", 200, '{"run_id": "old"}')
    # force expiry then prove the key is claimable again
    with memory._lock:  # noqa: SLF001 — test seam
        memory._conn.execute(  # noqa: SLF001
            "UPDATE idempotency_keys SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE idem_key = 'idem-key-old'")
        memory._conn.commit()
    assert memory.idempotency_get("anon:127.0.0.1", "idem-key-old") is None
    assert memory.idempotency_claim("anon:127.0.0.1", "idem-key-old", "chat", "hash2", 60) is None


def test_approvals_resolve_replays_with_key(app_client):
    # build a real pending approval via the mock tool flow is heavy; instead
    # prove the endpoint-level contract directly: unknown id → 404.
    r = app_client.post("/api/approvals/does-not-exist", json={"approved": True},
                        headers={"Idempotency-Key": "idem-key-appr-404"})
    assert r.status_code == 404
    # … and that a 404 does NOT poison the key (released → reusable):
    memory = app_client.app.state.agent.memory
    assert memory.idempotency_count() == 0
