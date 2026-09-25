"""Request IDs, Prometheus metrics, and cache policy (offline).

Every HTTP response — including 401/429 rejections — carries X-Request-ID
and is counted.  Metrics are dependency-free and per-process (documented);
/api/metrics stays behind API-key auth like every other non-public path.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# Fixture-only credential (never a real secret).  TestClient's client host is
# "testclient" (not loopback), so key auth is required even in development.
TEST_KEY = "test-key-telemetry-0123456789abcdefghij"


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


def test_request_id_is_generated_and_echoed(app_client):
    r = app_client.get("/api/health")
    assert r.status_code == 200
    assert len(r.headers["x-request-id"]) == 16


def test_valid_incoming_request_id_is_propagated(app_client):
    r = app_client.get("/api/health", headers={"X-Request-ID": "probe-123_ABC.def"})
    assert r.headers["x-request-id"] == "probe-123_ABC.def"


def test_malicious_request_id_is_replaced_not_echoed(app_client):
    evil = "x\"; <script>alert(1)</script>"
    r = app_client.get("/api/health", headers={"X-Request-ID": evil})
    assert r.headers["x-request-id"] != evil
    assert len(r.headers["x-request-id"]) == 16


def test_rejections_carry_request_id_and_metrics(app_client):
    from nimna.api.telemetry import get_registry

    r = app_client.get("/api/tools", headers={"X-Nimna-Key": "wrong-key-wrong-key-wrong-key-00"})
    assert r.status_code == 401
    assert "x-request-id" in r.headers
    assert get_registry().get("http_requests_total",
                              {"method": "GET", "path": "/api/tools", "status": "401"}) == 1.0


def test_metrics_endpoint_needs_auth_like_everything_else(app_client):
    r = app_client.get("/api/metrics", headers={"X-Nimna-Key": "wrong-key-wrong-key-wrong-key-00"})
    assert r.status_code == 401


def test_metrics_exposition_counts_requests_and_durations(app_client):
    from nimna.api.telemetry import get_registry

    app_client.get("/api/health")
    app_client.get("/api/health")
    reg = get_registry()
    body = reg.render_prometheus()
    assert 'http_requests_total{method="GET",path="/api/health",status="200"} 2' in body
    assert "http_request_duration_seconds_sum" in body
    assert "http_request_duration_seconds_count" in body
    assert "process_uptime_seconds" in body


def test_metrics_served_over_http(app_client):
    # A request is counted when its response *starts*, so the scrape itself is
    # never in its own body — make a prior request, then scrape.
    app_client.get("/api/health")
    r = app_client.get("/api/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_cache_policy_api_no_store_root_no_cache(app_client):
    assert app_client.get("/api/health").headers["cache-control"] == "no-store"
    assert app_client.get("/").headers["cache-control"] == "no-cache"


def test_path_normalization_keeps_labels_bounded():
    from nimna.api.telemetry import normalize_path

    assert normalize_path("/api/sessions/abc123/messages") == "/api/sessions/:id/messages"
    assert normalize_path("/api/approvals/xyz") == "/api/approvals/:id"
    assert normalize_path("/api/runs/r1/evidence") == "/api/runs/:id/evidence"
    assert normalize_path("/api/skills/reload") == "/api/skills/reload"
    assert normalize_path("/api/skills/csv_analysis") == "/api/skills/:id"
    assert normalize_path("/api/chat") == "/api/chat"
    assert normalize_path("/api/health") == "/api/health"
    assert normalize_path("/weird") == "/other"


def test_breaker_transitions_are_counted(app_client):
    from nimna.api.telemetry import get_registry

    breaker = app_client.app.state.agent.provider.circuit_breaker
    breaker._transition("open")  # noqa: SLF001 — drive the listener path directly
    breaker._transition("closed")  # noqa: SLF001
    reg = get_registry()
    assert reg.get("breaker_transitions_total",
                   {"breaker": breaker.name, "from_state": "closed", "to_state": "open"}) == 1.0
    assert reg.get("breaker_transitions_total",
                   {"breaker": breaker.name, "from_state": "open", "to_state": "closed"}) == 1.0


def test_health_reports_resilience_section(app_client):
    body = app_client.get("/api/health").json()
    resilience = body["resilience"]
    assert resilience["rate_limiter"]["backend"] == "memory"
    assert resilience["idempotency"]["ttl_seconds"] == 86400
    assert resilience["breaker"]["state"] == "closed"
    assert resilience["bulkhead"]["max_concurrent"] == 16
    assert isinstance(resilience["uptime_seconds"], int)
