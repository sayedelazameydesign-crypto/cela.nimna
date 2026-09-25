"""Approval policies for tools with risk == "confirm"."""
import json
import sys
from collections.abc import Callable
from typing import Protocol

from ..providers.base import ToolCall
from ..tools.base import Tool
from .state import Decision, RunState


class ApprovalPolicy(Protocol):
    def decide(self, state: RunState, tool: Tool, call: ToolCall) -> Decision: ...


class AutoApprove:
    """Approve everything. Only for trusted automation / tests."""

    def decide(self, state: RunState, tool: Tool, call: ToolCall) -> Decision:
        return Decision.APPROVE


class AlwaysDeny:
    def decide(self, state: RunState, tool: Tool, call: ToolCall) -> Decision:
        return Decision.DENY


class DeferToClient:
    """Suspend the run; the API client resolves it later via /approvals/{id}."""

    def decide(self, state: RunState, tool: Tool, call: ToolCall) -> Decision:
        return Decision.DEFER


class ConsolePrompt:
    """Interactive y/n/a prompt on the terminal (used by the CLI)."""

    def __init__(self, input_fn: Callable[[str], str] = input, output=None):
        self.input_fn = input_fn
        self.output = output or sys.stdout

    def decide(self, state: RunState, tool: Tool, call: ToolCall) -> Decision:
        args = json.dumps(call.arguments, ensure_ascii=False, indent=2)
        self.output.write(
            f"\n⚠️  Approval required / مطلوب موافقة: {tool.name}\n"
            f"    {tool.description}\n    arguments: {args}\n"
        )
        self.output.flush()
        while True:
            try:
                answer = self.input_fn("    [y] approve  [n] deny  [a] always for this tool > ").strip().lower()
            except EOFError:
                return Decision.DENY
            if answer in {"y", "yes", "نعم", "موافق"}:
                return Decision.APPROVE
            if answer in {"n", "no", "لا", "رفض"}:
                return Decision.DENY
            if answer in {"a", "always", "دائما", "دائمًا"}:
                return Decision.ALWAYS


class CallbackPolicy:
    """Adapter for custom UIs: ``CallbackPolicy(lambda state, tool, call: Decision.APPROVE)``."""

    def __init__(self, fn: Callable[[RunState, Tool, ToolCall], Decision]):
        self.fn = fn

    def decide(self, state: RunState, tool: Tool, call: ToolCall) -> Decision:
        return self.fn(state, tool, call)
