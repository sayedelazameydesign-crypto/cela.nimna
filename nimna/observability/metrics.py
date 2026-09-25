"""Dependency-free run metrics derived from canonical audit events."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass
class RunMetrics:
    run_id: str
    events: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    approvals: int = 0
    retries: int = 0
    elapsed_ms: int = 0
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "events": self.events,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "approvals": self.approvals,
            "retries": self.retries,
            "elapsed_ms": self.elapsed_ms,
            "tokens": self.tokens,
        }


def summarize_events(run_id: str, events: Iterable[Mapping[str, Any]]) -> RunMetrics:
    metrics = RunMetrics(run_id=run_id)
    for event in events:
        metrics.events += 1
        name = str(event.get("event", ""))
        payload = event.get("payload") or {}
        if name == "model_call":
            metrics.model_calls += 1
            usage = payload.get("usage") if isinstance(payload, dict) else {}
            if isinstance(usage, dict):
                metrics.tokens += sum(int(value) for value in usage.values() if isinstance(value, (int, float)))
        elif name == "tool_call":
            metrics.tool_calls += 1
        elif name == "tool_result" and isinstance(payload, dict) and not payload.get("ok", False):
            metrics.tool_failures += 1
        elif name == "approval_requested":
            metrics.approvals += 1
        elif "retry" in name:
            metrics.retries += 1
        elif name == "run_finished" and isinstance(payload, dict):
            metrics.elapsed_ms = int(float(payload.get("elapsed_ms", metrics.elapsed_ms) or 0))
    return metrics


__all__ = ["RunMetrics", "summarize_events"]
