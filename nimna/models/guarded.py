"""Provider decorator that enforces :class:`~nimna.models.registry.CostGuard`.

Keeping the guard at the provider boundary means planner, verifier, normal
agent turns, and swarm sub-agents all use the same budget gate.  The delegate is
still exposed through ``__getattr__`` for backwards-compatible tests and
integrations that inspect a provider's call history.
"""
from __future__ import annotations

from typing import Any, Optional

from ..providers.base import Message, ModelProvider, ModelResponse, ToolSpec
from .registry import BudgetReservation, CostGuard


class GovernedModelProvider(ModelProvider):
    """Transparent provider wrapper with a hard preflight cost check."""

    def __init__(self, delegate: ModelProvider, guard: CostGuard):
        self.delegate = delegate
        self.guard = guard
        self.name = getattr(delegate, "name", "provider")
        self.model = getattr(delegate, "model", "")

    def __getattr__(self, name: str) -> Any:
        # Preserve provider-specific attributes such as MockProvider.calls.
        return getattr(self.delegate, name)

    def describe(self) -> dict[str, Any]:
        data = dict(self.delegate.describe())
        data["cost_guard"] = self.guard.status()
        return data

    def generate(
        self,
        messages: list[Message],
        tools: Optional[list[ToolSpec]] = None,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> ModelResponse:
        reservation: BudgetReservation = self.guard.authorize(messages, max_tokens or 0)
        try:
            try:
                response = self.delegate.generate(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except TypeError:
                # A small third-party provider may not support optional kwargs.
                response = self.delegate.generate(messages, tools=tools)
        except Exception:
            self.guard.settle(reservation, None)
            raise
        self.guard.settle(reservation, response.usage)
        return response


__all__ = ["GovernedModelProvider"]
