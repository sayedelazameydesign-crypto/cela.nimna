"""Governed Browser Use Cloud V4 tool.

This tool is intentionally opt-in and confirm-gated.  Browser Use Cloud is pay
as you go, so the local hard gate refuses to call it when the configured
browser budget is zero.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ...browser import BrowserUseV4Client
from ..base import ToolError, ToolRegistry


class BrowserUseRunParams(BaseModel):
    task: str = Field(..., min_length=1, max_length=8000, description="Task for the hosted Browser Use agent.")
    model: Optional[str] = Field(None, description="Optional model id accepted by the Cloud V4 API.")
    reasoning_effort: Optional[str] = Field(None, description="Model-specific reasoning value, e.g. xhigh for GPT-6 Astra.")
    timeout_seconds: int = Field(120, ge=5, le=1800, description="Client wait timeout; it does not cancel a server-side run.")


def register(registry: ToolRegistry) -> ToolRegistry:
    @registry.tool(
        "browser_use_run",
        "Run a task with Browser Use Cloud API V4. Disabled unless explicitly enabled and budgeted.",
        BrowserUseRunParams,
        risk="confirm",
        tags=["browser", "external-network", "paid-service"],
    )
    def browser_use_run(params: BrowserUseRunParams, ctx):  # type: ignore[no-untyped-def]
        settings = ctx.settings
        if not getattr(settings, "browser_use_enabled", False):
            raise ToolError("Browser Use Cloud is disabled (set BROWSER_USE_ENABLED=true explicitly)")
        if float(getattr(settings, "browser_use_max_spend_usd", 0.0)) <= 0:
            raise ToolError(
                "Browser Use Cloud call blocked by the hard zero-spend gate; "
                "set BROWSER_USE_MAX_SPEND_USD to an explicit positive budget"
            )
        api_key = getattr(settings, "browser_use_api_key", None)
        if not api_key:
            raise ToolError("BROWSER_USE_API_KEY is not configured")
        client = BrowserUseV4Client(
            api_key=api_key,
            base_url=getattr(settings, "browser_use_base_url", "https://api.browser-use.com"),
            timeout=float(getattr(settings, "browser_use_timeout", 120.0)),
            poll_interval=float(getattr(settings, "browser_use_poll_interval", 2.0)),
        )
        try:
            options = {}
            if params.model:
                options["model"] = params.model
            reasoning_effort = params.reasoning_effort or getattr(settings, "browser_use_reasoning_effort", None)
            if reasoning_effort:
                options["reasoning_effort"] = reasoning_effort
            result = client.run_agent_task(params.task, timeout=params.timeout_seconds, **options)
            # Keep the tool result compact; the complete run remains in the
            # provider response/audit path and can be reconciled by run_id.
            return {
                "run_id": result.get("run_id"),
                "status": result.get("status"),
                "result": result.get("result"),
            }
        finally:
            client.close()

    return registry
