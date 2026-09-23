"""Offline contract tests for the Agent OS boundaries added in this sprint."""
from __future__ import annotations

import httpx
import pytest

from nimna.browser import BrowserUseRateLimitError, BrowserUseV4Client
from nimna.config import Settings
from nimna.memory import MemoryStore
from nimna.models import BudgetExceededError, CostGuard, CostProfile, ModelProfile


def test_zero_budget_allows_declared_zero_cost_and_blocks_unknown():
    free = ModelProfile("mock", "mock", cost=CostProfile(0, 0, known=True))
    guard = CostGuard(free, max_spend_usd=0)
    reservation = guard.authorize([], max_output_tokens=100)
    guard.settle(reservation, {"prompt_tokens": 1, "completion_tokens": 1})
    assert guard.status()["remaining_usd"] == 0

    unknown = ModelProfile("future", "future", cost=CostProfile())
    with pytest.raises(BudgetExceededError, match="pricing is unknown"):
        CostGuard(unknown, max_spend_usd=0).authorize([], max_output_tokens=1)


def test_audit_evidence_chain_detects_tampering():
    store = MemoryStore(":memory:")
    store.log("session", "run", "started", {"answer": "ok"})
    store.log("session", "run", "finished", {"answer": "done"})
    assert store.verify_audit_chain(run_id="run")["valid"] is True
    with store._lock:  # deliberate test-only tamper of the canonical store
        store._conn.execute("UPDATE audit_log SET payload = ? WHERE event = 'finished'", ('{"answer":"tampered"}',))
    assert store.verify_audit_chain(run_id="run")["valid"] is False


def test_browser_v4_auth_and_owned_browser_cleanup():
    seen: list[tuple[str, str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("X-Browser-Use-API-Key"), request.headers.get("Authorization")))
        if request.method == "POST" and request.url.path == "/api/v4/browsers":
            return httpx.Response(200, json={"id": "browser-1", "cdpUrl": "wss://cdp"})
        if request.method == "PATCH" and request.url.path == "/api/v4/browsers/browser-1":
            return httpx.Response(200, json={"status": "stopped"})
        return httpx.Response(404, json={"message": "not found"})

    transport = httpx.MockTransport(handler)
    with BrowserUseV4Client(
        "test-secret",
        http_client=httpx.Client(base_url="https://api.browser-use.com", transport=transport),
    ) as client:
        with client.managed_browser(proxy_country_code="us") as session:
            assert session.id == "browser-1"
    assert seen == [
        ("POST", "/api/v4/browsers", "test-secret", None),
        ("PATCH", "/api/v4/browsers/browser-1", "test-secret", None),
    ]


def test_browser_v4_rejects_invalid_astra_reasoning():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "run-1", "status": "queued"})

    with BrowserUseV4Client(
        "key",
        http_client=httpx.Client(base_url="https://api.browser-use.com", transport=httpx.MockTransport(handler)),
    ) as client:
        with pytest.raises(ValueError, match="GPT-6 Astra"):
            client.create_run("task", model="GPT-6 Astra", reasoning_effort="minimal")


def test_browser_v4_exposes_retry_after_without_guessing_rps():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7", "X-RateLimit-Limit": "125"}, json={"error": "busy"})

    with BrowserUseV4Client(
        "key",
        http_client=httpx.Client(base_url="https://api.browser-use.com", transport=httpx.MockTransport(handler)),
    ) as client:
        with pytest.raises(BrowserUseRateLimitError) as error:
            client.account()
    assert error.value.retry_after_seconds == 7
    assert client._limiter.limit == 125


def test_runtime_settings_default_to_hard_zero_spend():
    settings = Settings.from_env(env_file=None)
    assert settings.cost_guard_enabled is True
    assert settings.cost_guard_hard is True
    assert settings.max_spend_usd == 0
    assert settings.browser_use_enabled is False
    assert settings.browser_use_max_spend_usd == 0
