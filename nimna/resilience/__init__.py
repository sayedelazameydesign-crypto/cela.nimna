"""Failure-containment primitives (dependency-free, thread-safe).

* :class:`CircuitBreaker` — stop hammering a dead model vendor after N
  consecutive failures; half-open probing re-admits traffic automatically.
* :class:`Bulkhead` — cap concurrent model calls per process so one slow
  vendor cannot pile up every worker thread.

Both live at the provider boundary (see :class:`GovernedModelProvider`):
inside the cost guard, so a *budget* refusal never trips the breaker, and
outside the vendor SDKs, so planner / verifier / agent turns / swarm share
one policy.  Docs: ``docs/RESILIENCE.md``.
"""
from .circuit import (
    Bulkhead,
    BulkheadFullError,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)

__all__ = [
    "Bulkhead",
    "BulkheadFullError",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
]
