"""Circuit breaker + bulkhead for outbound model calls.

Design rules (kept boring on purpose):

* **One recording per model turn.**  The breaker's ``call()`` wraps the whole
  ``generate()`` — including the provider's internal retries — so a turn that
  succeeds after two 429s counts as a success, not two failures.
* **Fail fast, fail honest.**  An open breaker raises :class:`CircuitOpenError`
  (a :class:`ProviderError`, so existing ``except ProviderError`` handling
  keeps working) carrying ``retry_after`` seconds for the HTTP layer.
* **No silent sharing.**  Breakers are explicit constructor arguments, never
  process-global singletons — tests and multi-agent processes stay isolated.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional, TypeVar

from ..providers.base import ProviderError

T = TypeVar("T")

CircuitState = str  # "closed" | "open" | "half_open"


class CircuitOpenError(ProviderError):
    """The breaker is open: the vendor recently failed N consecutive times."""

    def __init__(self, breaker: str, retry_after: float):
        super().__init__(
            f"circuit breaker '{breaker}' is open — vendor failing, "
            f"retry in {retry_after:.0f}s",
            retryable=False,
        )
        self.breaker = breaker
        self.retry_after = max(retry_after, 0.0)


class BulkheadFullError(ProviderError):
    """Too many concurrent model calls; the caller should retry shortly."""

    def __init__(self, name: str, limit: int):
        super().__init__(
            f"bulkhead '{name}' full ({limit} concurrent model calls) — retry shortly",
            retryable=True,
        )
        self.bulkhead = name


class CircuitBreaker:
    """Classic closed/open/half-open breaker over consecutive failures."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        ignore: tuple[type[BaseException], ...] = (),
    ):
        self.name = name
        self.failure_threshold = max(int(failure_threshold), 1)
        self.cooldown_seconds = max(float(cooldown_seconds), 0.0)
        self._clock = clock
        self._ignore = ignore
        self._lock = threading.Lock()
        self._state: CircuitState = "closed"
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_probe = False
        self.success_count = 0
        self.failure_count = 0
        self.rejection_count = 0
        # Transition listeners: fn(name, from_state, to_state).  Used by the
        # telemetry layer; never let a listener break the guarded call.
        self.on_transition: list[Callable[[str, str, str], None]] = []

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _transition(self, to_state: CircuitState) -> None:
        from_state = self._state
        if from_state == to_state:
            return
        self._state = to_state
        for listener in list(self.on_transition):
            try:
                listener(self.name, from_state, to_state)
            except Exception:
                pass

    def _maybe_half_open(self) -> None:
        # Lock must be held.
        if self._state == "open" and (self._clock() - self._opened_at) >= self.cooldown_seconds:
            self._transition("half_open")
            self._half_open_probe = False

    def _record_success(self) -> None:
        with self._lock:
            self.success_count += 1
            self._consecutive_failures = 0
            self._half_open_probe = False
            if self._state != "closed":
                self._transition("closed")

    def _record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self._consecutive_failures += 1
            self._half_open_probe = False
            if self._state == "half_open" or (
                self._state == "closed" and self._consecutive_failures >= self.failure_threshold
            ):
                self._opened_at = self._clock()
                self._transition("open")

    def call(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` under the breaker.  Thread-safe."""
        with self._lock:
            self._maybe_half_open()
            if self._state == "open":
                self.rejection_count += 1
                retry_after = self.cooldown_seconds - (self._clock() - self._opened_at)
                raise CircuitOpenError(self.name, retry_after)
            if self._state == "half_open":
                if self._half_open_probe:
                    # One probe at a time; the rest fail fast.
                    self.rejection_count += 1
                    raise CircuitOpenError(self.name, self.cooldown_seconds)
                self._half_open_probe = True
        try:
            result = fn()
        except self._ignore:
            raise
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state,
                "failure_threshold": self.failure_threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "consecutive_failures": self._consecutive_failures,
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "rejection_count": self.rejection_count,
            }

    def reset(self) -> None:
        """Force closed (tests / operator recovery). Counters are kept."""
        with self._lock:
            self._consecutive_failures = 0
            self._half_open_probe = False
            self._transition("closed")


class Bulkhead:
    """Semaphore bulkhead: at most ``max_concurrent`` guarded calls at once."""

    def __init__(self, name: str, max_concurrent: int = 16, acquire_timeout: float = 30.0):
        self.name = name
        self.max_concurrent = max(int(max_concurrent), 1)
        self.acquire_timeout = max(float(acquire_timeout), 0.0)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self._lock = threading.Lock()
        self.rejection_count = 0
        self.admitted_count = 0

    def call(self, fn: Callable[[], T], *, timeout: Optional[float] = None) -> T:
        acquired = self._semaphore.acquire(
            blocking=True, timeout=self.acquire_timeout if timeout is None else timeout
        )
        if not acquired:
            with self._lock:
                self.rejection_count += 1
            raise BulkheadFullError(self.name, self.max_concurrent)
        with self._lock:
            self.admitted_count += 1
        try:
            return fn()
        finally:
            self._semaphore.release()

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_concurrent": self.max_concurrent,
            "admitted_count": self.admitted_count,
            "rejection_count": self.rejection_count,
        }


__all__ = [
    "Bulkhead",
    "BulkheadFullError",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
]
