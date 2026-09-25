"""Circuit breaker + bulkhead + retry jitter (offline, no vendors).

The breaker lives at the provider boundary: after N consecutive model
failures it short-circuits instead of hammering a dead vendor, half-open
probing re-admits traffic, and the bulkhead caps concurrent model calls so
one slow vendor cannot pile up every worker thread.
"""
from __future__ import annotations

import threading

import pytest

from nimna.models.guarded import GovernedModelProvider
from nimna.models.registry import CostGuard
from nimna.providers.base import ModelResponse, ProviderError, with_retries
from nimna.providers.mock import MockProvider
from nimna.resilience import (
    Bulkhead,
    BulkheadFullError,
    CircuitBreaker,
    CircuitOpenError,
)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


def _boom():
    raise ProviderError("vendor down", retryable=True)


def _ok():
    return "fine"


# -- breaker lifecycle -----------------------------------------------------


def test_closed_breaker_passes_calls_through():
    breaker = CircuitBreaker("t", failure_threshold=2, cooldown_seconds=60)
    assert breaker.call(_ok) == "fine"
    assert breaker.state == "closed"
    assert breaker.stats()["success_count"] == 1


def test_breaker_opens_after_threshold_consecutive_failures():
    clock = _Clock()
    breaker = CircuitBreaker("t", failure_threshold=3, cooldown_seconds=60, clock=clock)
    for _ in range(2):
        with pytest.raises(ProviderError):
            breaker.call(_boom)
    assert breaker.state == "closed"
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    assert breaker.state == "open"
    # short-circuit: the function is not even invoked
    calls = []
    with pytest.raises(CircuitOpenError) as excinfo:
        breaker.call(lambda: calls.append(1))
    assert calls == []
    assert 0 < excinfo.value.retry_after <= 60
    assert isinstance(excinfo.value, ProviderError)  # existing handling keeps working


def test_success_resets_the_consecutive_counter():
    breaker = CircuitBreaker("t", failure_threshold=2, cooldown_seconds=60)
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    assert breaker.call(_ok) == "fine"
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    assert breaker.state == "closed", "one failure after a success must not open (threshold=2)"


def test_half_open_probe_closes_on_success():
    clock = _Clock()
    breaker = CircuitBreaker("t", failure_threshold=1, cooldown_seconds=30, clock=clock)
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    assert breaker.state == "open"
    clock.advance(31)
    assert breaker.state == "half_open"
    assert breaker.call(_ok) == "fine"
    assert breaker.state == "closed"


def test_half_open_probe_reopens_on_failure():
    clock = _Clock()
    breaker = CircuitBreaker("t", failure_threshold=1, cooldown_seconds=30, clock=clock)
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    clock.advance(31)
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    assert breaker.state == "open"


def test_only_one_half_open_probe_at_a_time():
    clock = _Clock()
    breaker = CircuitBreaker("t", failure_threshold=1, cooldown_seconds=30, clock=clock)
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    clock.advance(31)
    gate = threading.Event()
    release = threading.Event()
    results: list[str] = []

    def slow_probe():
        gate.set()
        assert release.wait(timeout=5)
        return "probe"

    def run_probe():
        try:
            results.append(breaker.call(slow_probe))
        except CircuitOpenError:
            results.append("rejected")

    first = threading.Thread(target=run_probe)
    first.start()
    assert gate.wait(timeout=5)
    second = threading.Thread(target=run_probe)
    second.start()
    second.join(timeout=5)
    release.set()
    first.join(timeout=5)
    assert sorted(results) == ["probe", "rejected"]


def test_ignored_exceptions_are_not_counted():
    breaker = CircuitBreaker("t", failure_threshold=1, ignore=(KeyboardInterrupt,))
    with pytest.raises(KeyboardInterrupt):
        breaker.call(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert breaker.state == "closed"
    assert breaker.stats()["failure_count"] == 0


def test_transition_listeners_fire_but_cannot_break_calls():
    breaker = CircuitBreaker("t", failure_threshold=1, cooldown_seconds=60)
    seen: list[tuple[str, str, str]] = []
    breaker.on_transition.append(lambda n, f, to: seen.append((n, f, to)))
    breaker.on_transition.append(lambda *a: (_ for _ in ()).throw(RuntimeError("listener bug")))
    with pytest.raises(ProviderError):
        breaker.call(_boom)
    assert seen == [("t", "closed", "open")]


# -- bulkhead --------------------------------------------------------------


def test_bulkhead_caps_concurrency_and_fails_fast():
    bulkhead = Bulkhead("t", max_concurrent=1, acquire_timeout=0.05)
    gate = threading.Event()
    release = threading.Event()

    def slow():
        gate.set()
        assert release.wait(timeout=5)
        return "slow"

    worker = threading.Thread(target=lambda: bulkhead.call(slow))
    worker.start()
    assert gate.wait(timeout=5)
    with pytest.raises(BulkheadFullError):
        bulkhead.call(_ok)
    release.set()
    worker.join(timeout=5)
    assert bulkhead.call(_ok) == "fine"
    assert bulkhead.stats()["rejection_count"] == 1


def test_bulkhead_releases_on_exception():
    bulkhead = Bulkhead("t", max_concurrent=1)
    with pytest.raises(ProviderError):
        bulkhead.call(_boom)
    assert bulkhead.call(_ok) == "fine"  # slot was released


# -- retry jitter ----------------------------------------------------------


def test_with_retries_jitter_stays_within_backoff_bounds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("nimna.providers.base.time.sleep", sleeps.append)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ProviderError("429", retryable=True)
        return "recovered"

    assert with_retries(flaky, attempts=3, base_delay=2.0, max_delay=30.0) == "recovered"
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 2.0, sleeps
    assert 0 <= sleeps[1] <= 4.0, sleeps


def test_with_retries_jitter_can_be_disabled(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("nimna.providers.base.time.sleep", sleeps.append)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 2:
            raise ProviderError("500", retryable=True)
        return "ok"

    assert with_retries(flaky, attempts=2, base_delay=2.0, jitter=False) == "ok"
    assert sleeps == [2.0]


def test_with_retries_never_retries_non_retryable():
    calls = []

    def fatal():
        calls.append(1)
        raise ProviderError("bad key", retryable=False)

    with pytest.raises(ProviderError):
        with_retries(fatal, attempts=3)
    assert len(calls) == 1


# -- provider-boundary wiring ----------------------------------------------


def _governed(provider=None, **kwargs):
    from nimna.config import Settings

    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    provider = provider or MockProvider()
    guard = CostGuard.from_settings(settings, provider.describe())
    return GovernedModelProvider(provider, guard, **kwargs)


def test_breaker_counts_one_recording_per_turn_not_per_retry():
    from nimna.providers.base import Message

    breaker = CircuitBreaker("t", failure_threshold=5, cooldown_seconds=60)
    governed = _governed(circuit_breaker=breaker)
    governed.generate([Message.user("hi")])
    assert breaker.stats()["success_count"] == 1
    assert breaker.stats()["failure_count"] == 0


def test_consecutive_turn_failures_open_the_provider_breaker():
    from nimna.providers.base import Message

    class AlwaysDown(MockProvider):
        def generate(self, *a, **k):
            raise ProviderError("vendor down", retryable=True)

    breaker = CircuitBreaker("t", failure_threshold=3, cooldown_seconds=60)
    governed = _governed(AlwaysDown(), circuit_breaker=breaker)
    for _ in range(3):
        with pytest.raises(ProviderError):
            governed.generate([Message.user("hi")])
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        governed.generate([Message.user("hi")])
    assert governed.resilience_status()["breaker"]["state"] == "open"


def test_budget_refusal_never_trips_the_breaker():
    from nimna.config import Settings
    from nimna.models import BudgetExceededError
    from nimna.models.registry import CostGuard
    from nimna.providers.base import Message

    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    settings.max_spend_usd = 0.0
    provider = MockProvider()
    guard = CostGuard.from_settings(settings, provider.describe())
    breaker = CircuitBreaker("t", failure_threshold=1, cooldown_seconds=60)
    governed = GovernedModelProvider(provider, guard, circuit_breaker=breaker)
    # Force the guard to refuse everything: pretend the budget is exhausted.
    guard.spent_usd = 999.0
    with pytest.raises(BudgetExceededError):
        governed.generate([Message.user("hi")])
    assert breaker.state == "closed"
    assert breaker.stats()["failure_count"] == 0


def test_agent_wires_breaker_from_settings():
    from nimna.config import Settings
    from nimna.core.agent import Agent
    from nimna.memory import MemoryStore
    from nimna.skills import SkillManager
    from nimna.tools import default_registry

    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    settings.db_path = __import__("pathlib").Path(":memory:")
    settings.breaker_failure_threshold = 7
    settings.breaker_cooldown_seconds = 11
    settings.bulkhead_max_concurrent = 3
    agent = Agent(
        MockProvider(),
        SkillManager("skills"),
        default_registry(),
        MemoryStore(":memory:"),
        settings,
    )
    status = agent.provider.resilience_status()
    assert status["breaker"]["failure_threshold"] == 7
    assert status["breaker"]["cooldown_seconds"] == 11
    assert status["bulkhead"]["max_concurrent"] == 3
