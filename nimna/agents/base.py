"""Base Swarm Agent — unified contract for Search/Code/Vision sub-agents.

Each agent is a constrained Agent-loop:
  - isolated allowed_tools
  - own system_prompt (role)
  - shared provider / memory / vector-memory / workspace
  - audit trail via parent memory.store (audit_log)

The agent executes a synchronous loop (max_steps) but is invoked via asyncio.to_thread
by PlannerSwarm for parallelism.

Self-Healing is implemented at CodeAgent level (override after_tool_error).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings
from ..memory.store import MemoryStore
from ..providers.base import Message, ModelProvider
from ..tools.base import ToolContext, ToolRegistry

log = logging.getLogger(__name__)


class SwarmResult:
    def __init__(self, agent: str, task: str, ok: bool, output: str, tool_calls: list[dict], usage: dict, error: str | None = None):
        self.agent = agent
        self.task = task
        self.ok = ok
        self.output = output
        self.tool_calls = tool_calls
        self.usage = usage
        self.error = error
        self.duration_ms = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "task": self.task,
            "ok": self.ok,
            "output": self.output[:4000],
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "error": self.error,
        }


class BaseSwarmAgent:
    name: str = "base"
    description: str = ""
    allowed_tools: list[str] = []
    system_prompt: str = ""

    def __init__(
        self,
        provider: ModelProvider,
        settings: Settings,
        memory: MemoryStore,
        tools: ToolRegistry,
        *,
        workspace: Any = None,
        session_id: str = "swarm",
        execution_gateway: Any = None,
    ):
        self.provider = provider
        self.settings = settings
        self.memory = memory
        self.tools = tools
        # P1-T7.1 binding: None (default) = declared legacy compatibility;
        # when bound, the strict three-way routing below applies.
        self.execution_gateway = execution_gateway
        self.workspace = workspace or settings.workspace_dir
        self.session_id = session_id

    def _context(self, run_id: str) -> ToolContext:
        return ToolContext(
            settings=self.settings,
            workspace=self.workspace,
            session_id=self.session_id,
            memory=self.memory,
            skills=None,  # type: ignore
            run_id=run_id,
            on_skill_loaded=lambda x: None,
        )

    def _system_message(self, task: str, context: dict | None = None) -> str:
        ctx = ""
        if context and context.get("shared_results"):
            ctx = "\n\n## Shared context from sibling agents\n" + "\n".join(
                f"- {k}: {str(v)[:500]}" for k, v in context["shared_results"].items()
            )
        # inject vector memory hint (self-healing context) if available
        return f"{self.system_prompt}\n\nTask: {task}\n{ctx}\n\nConstraints: Use only tools {', '.join(self.allowed_tools) or 'none'}. Answer in the language of the task (Arabic if Arabic). Be concise but cite tool outputs."

    def run(self, task: str, session_id: str | None = None, context: dict | None = None) -> SwarmResult:
        """Synchronous bounded loop — called via to_thread for parallelism."""
        import uuid

        session_id = session_id or self.session_id
        run_id = f"swarm-{self.name}-{uuid.uuid4().hex[:8]}"
        start = time.perf_counter()
        allowed = list(self.allowed_tools)
        # include memory tools always if not already
        tools_spec = self.tools.specs(allowed) if allowed else None

        messages: list[Message] = [
            Message.system(self._system_message(task, context)),
            Message.user(task),
        ]
        tool_calls_log: list[dict] = []
        usage: dict[str, int] = {}
        step = 0
        max_steps = min(self.settings.max_steps, 8)  # swarm agents are short-lived

        while step < max_steps:
            step += 1
            try:
                resp = self.provider.generate(messages, tools=tools_spec or None, max_tokens=self.settings.max_response_tokens)
            except Exception as exc:  # ProviderError etc.
                return SwarmResult(self.name, task, False, f"provider error: {exc}", tool_calls_log, usage, error=str(exc))

            # usage
            for k, v in (resp.usage or {}).items():
                usage[k] = usage.get(k, 0) + int(v)

            messages.append(resp.to_message())

            if not resp.tool_calls:
                txt = (resp.text or "").strip()
                # success
                elapsed = int((time.perf_counter() - start) * 1000)
                # audit
                try:
                    self.memory.log(session_id, run_id, f"swarm:{self.name}:done", {"task": task[:200], "steps": step})
                except Exception:
                    pass
                res = SwarmResult(self.name, task, True, txt or "(no output)", tool_calls_log, usage)
                res.duration_ms = elapsed
                return res

            # execute tool calls
            for call in resp.tool_calls:
                tool = self.tools.get(call.name)
                if tool is None or call.name not in allowed:
                    # tool not allowed → error to model
                    err = f"tool '{call.name}' not allowed for {self.name}. Allowed: {allowed}"
                    messages.append(Message.tool_result(call, f'{{"error":"{err}"}}'))
                    tool_calls_log.append({"name": call.name, "ok": False, "error": err})
                    continue
                # validate
                try:
                    tool.validate(call.arguments)  # validate يرفع عند الوسائط غير الصالحة — النداء هو التحقق
                except Exception as exc:
                    msg = str(exc)
                    messages.append(Message.tool_result(call, f'{{"error": {msg!r}}}'))
                    tool_calls_log.append({"name": call.name, "ok": False, "error": msg})
                    continue
                # run — T7.1 strict routing (mirrors Agent._run_tool):
                # gateway-registered → fabric · explicitly compat-listed →
                # legacy · otherwise REFUSED (fail-closed: no handler).
                gateway = getattr(self, "execution_gateway", None)
                if gateway is not None and gateway.has(call.name):
                    import json as _json
                    started = time.perf_counter()
                    outcome = gateway.invoke_for_agent(
                        call.name, call.arguments,
                        actor=f"swarm:{self.name}", session_id=run_id, mission_id=run_id,
                        resource=str(call.arguments.get("resource") or "workspace"),
                        requested_operation=call.name,
                    )
                    payload = {"status": outcome.execution_status, "ok": outcome.ok,
                               "reason": outcome.reason[:200],
                               "result": outcome.result if outcome.handler_called else None,
                               "evidence_digest": (outcome.record or {}).get("hash", "")}
                    result = _json.dumps(payload, ensure_ascii=False, default=str)[: self.settings.tool_result_max_chars]
                    ok, dur = outcome.ok, int((time.perf_counter() - started) * 1000)
                elif gateway is not None and call.name not in getattr(gateway, "compat_tools", frozenset()):
                    err = (f"tool '{call.name}' is not registered in the bound execution "
                           "gateway (fail-closed binding)")
                    messages.append(Message.tool_result(call, f'{{"error":"{err}"}}'))
                    tool_calls_log.append({"name": call.name, "ok": False, "error": err})
                    continue
                else:
                    result, ok, dur = self.tools.execute(call.name, call.arguments, self._context(run_id), max_chars=self.settings.tool_result_max_chars)
                messages.append(Message.tool_result(call, result))
                tool_calls_log.append({"name": call.name, "ok": ok, "dur": dur, "preview": result[:300]})
                # self-healing hook (overridden in CodeAgent)
                if not ok:
                    healed = self._on_tool_error(task, call, result, messages, context)
                    if healed:
                        # healed signals we want to continue loop; provider will see new message
                        pass

        elapsed = int((time.perf_counter() - start) * 1000)
        res = SwarmResult(self.name, task, False, "max_steps reached without final answer", tool_calls_log, usage, error="max_steps")
        res.duration_ms = elapsed
        return res

    # hook for self-healing (CodeAgent overrides)
    def _on_tool_error(self, task: str, call: Any, result: str, messages: list[Message], context: dict | None) -> bool:
        return False

    # async wrapper
    async def arun(self, task: str, session_id: str | None = None, context: dict | None = None) -> SwarmResult:
        import asyncio

        # Use get_running_loop().run_in_executor to avoid blocking
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.run(task, session_id=session_id, context=context))
