"""The agent loop.

    user request
      -> planner selects skills (catalog only)
      -> full SKILL.md bodies loaded into the system prompt
      -> tools restricted to what the loaded skills allow
      -> model <-> tool loop (validation, approvals, audit)
      -> optional verification pass
      -> final answer persisted to session memory

Runs are serialisable (:class:`RunState`), so a run can pause when a tool
needs approval and be resumed later by :meth:`Agent.resume`.
"""
import json
import logging
import uuid
from typing import Any, Optional

from ..config import Settings
from ..memory.store import MemoryStore
from ..providers.base import Message, ModelProvider, ProviderError, ToolCall
from ..skills.manager import SkillManager
from ..tools.base import Tool, ToolContext, ToolRegistry, ToolValidationError, serialize_result
from .approval import ApprovalPolicy, AutoApprove, DeferToClient
from .planner import SkillSelector, extract_json
from .state import AgentResult, Decision, PendingApproval, RunState, RunStatus, ToolCallRecord

log = logging.getLogger(__name__)

CORE_TOOLS = ["load_skill", "read_skill_reference", "memory_search", "memory_save"]

BASE_SYSTEM_PROMPT = """You are Nimna, a reusable-skills agent. You solve tasks by following the loaded skill
instructions and calling the tools exposed to you.

Operating rules:
1. Answer in the language of the user's message (Arabic requests get Arabic answers).
2. Before calling a tool, state briefly (one short sentence) which tool you will use and why.
3. Use only the tools available in this turn. If a needed capability is missing, say so and, if a
   relevant skill exists, load it with load_skill.
4. Never delete, overwrite, send, purchase or execute anything without the user's explicit consent.
   The system will ask the user for approval on sensitive tools; if a tool result says the action
   was denied, do NOT retry it – explain and offer alternatives.
5. Never modify the user's original files unless explicitly asked; write outputs to new files.
6. Do not invent tool results. If a tool fails, report the error honestly and try a sensible fix.
7. Work step by step and, when finished, give a clear final answer (tables for tabular data,
   file paths for anything you wrote).
8. All file paths are relative to the workspace directory.
"""


class Agent:
    def __init__(self, provider: ModelProvider, skills: SkillManager, tools: ToolRegistry,
                 memory: MemoryStore, settings: Settings,
                 approval_policy: Optional[ApprovalPolicy] = None, *,
                 workspace: Optional[Any] = None, use_llm_planner: bool = True):
        self.provider = provider
        self.skills = skills
        self.tools = tools
        self.memory = memory
        self.settings = settings
        self.workspace = (workspace or settings.workspace_dir)
        self.approval_policy: ApprovalPolicy = approval_policy or (
            AutoApprove() if settings.auto_approve else DeferToClient()
        )
        self.selector = SkillSelector(provider, skills, max_skills=settings.max_skills,
                                      use_llm=use_llm_planner)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def run(self, user_message: str, session_id: Optional[str] = None) -> AgentResult:
        session_id = session_id or uuid.uuid4().hex[:12]
        self.memory.ensure_session(session_id)
        state = RunState(session_id=session_id, user_message=user_message)
        self._audit(state, "run_started", {"message": user_message[:500]})
        self.memory.add_message(session_id, "user", user_message, {"run_id": state.run_id})
        try:
            self._prepare(state)
            return self._drive(state)
        except ProviderError as exc:
            return self._fail(state, f"model provider error: {exc}")
        except Exception as exc:  # pragma: no cover - last resort
            log.exception("agent run crashed")
            return self._fail(state, f"internal error: {type(exc).__name__}: {exc}")

    def resume(self, run_id: str, approved: bool, *, always: bool = False) -> AgentResult:
        """Resolve a pending approval and continue the suspended run."""
        raw = self.memory.get_pending(run_id)
        if raw is None:
            raise KeyError(f"no pending approval for run '{run_id}'")
        state = RunState.model_validate(raw)
        decision = Decision.ALWAYS if (approved and always) else (Decision.APPROVE if approved else Decision.DENY)
        self.memory.resolve_pending(run_id, decision.value)
        self._audit(state, "approval_resolved", {"tool": state.pending.tool_name if state.pending else None,
                                                  "decision": decision.value})
        try:
            self._apply_decision(state, decision)
            if state.status == RunStatus.AWAITING_APPROVAL:
                return self._suspend(state)
            outcome = self._execute_calls(state, start_index=state.pending_call_index + 1)
            if outcome == "deferred":
                return self._suspend(state)
            state.status = RunStatus.RUNNING
            return self._drive(state)
        except ProviderError as exc:
            return self._fail(state, f"model provider error: {exc}")

    def pending_approvals(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        return self.memory.list_pending(session_id)

    # ------------------------------------------------------------------
    # preparation
    # ------------------------------------------------------------------
    def _history(self, session_id: str) -> list[Message]:
        rows = self.memory.get_messages(session_id, limit=self.settings.history_messages + 1)
        history: list[Message] = []
        for row in rows[:-1]:  # last row is the message we just stored
            if row["role"] in {"user", "assistant"} and row["content"]:
                history.append(Message(role=row["role"], content=row["content"]))
        return history

    def _prepare(self, state: RunState) -> None:
        history = self._history(state.session_id)
        selection = self.selector.select(state.user_message, history)
        state.selection_reason = selection.reason
        state.plan = selection.plan
        self._audit(state, "skills_selected", {"skills": selection.skills, "reason": selection.reason,
                                               "source": selection.source})
        for name in selection.skills:
            self._load_skill(state, name)
        self._refresh_allowed_tools(state)
        state.messages = [Message.system(self._system_prompt(state))] + history + [Message.user(state.user_message)]

    def _load_skill(self, state: RunState, name: str) -> None:
        if name in state.loaded_skills or name not in self.skills:
            return
        state.loaded_skills.append(name)
        skill = self.skills.get(name)
        self._audit(state, "skill_loaded", {"skill": name, "version": skill.meta.version,
                                            "allowed_tools": skill.meta.allowed_tools})

    def _refresh_allowed_tools(self, state: RunState) -> None:
        allowed: list[str] = []
        for name in CORE_TOOLS:
            if name in self.tools and name not in allowed:
                allowed.append(name)
        if state.loaded_skills:
            for skill_name in state.loaded_skills:
                for tool_name in self.skills.get(skill_name).meta.allowed_tools:
                    if tool_name in self.tools and tool_name not in allowed:
                        allowed.append(tool_name)
                    elif tool_name not in self.tools:
                        log.warning("skill %s references unknown tool %s", skill_name, tool_name)
        else:
            for tool_name in self.settings.default_tools:
                if tool_name in self.tools and tool_name not in allowed:
                    allowed.append(tool_name)
        state.allowed_tools = allowed

    def _system_prompt(self, state: RunState) -> str:
        sections = [BASE_SYSTEM_PROMPT.strip()]
        if state.loaded_skills:
            blocks = [self.skills.get(name).as_prompt_block() for name in state.loaded_skills]
            sections.append("## Loaded skills\n\n" + "\n\n".join(blocks))
        else:
            sections.append(
                "## Loaded skills\n\n(none – handle the request directly; call load_skill if one of the "
                "installed skills fits)\n\nInstalled skills:\n" + self.skills.catalog_text()
            )
        if state.plan:
            sections.append("## Suggested plan\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(state.plan)))
        preferences = self.memory.list_memories(kind="preference", limit=10)
        if preferences:
            sections.append("## Known user preferences\n" + "\n".join(f"- {p['content']}" for p in preferences))
        sections.append("## Tools available this turn\n" + (", ".join(state.allowed_tools) or "(none)"))
        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def _drive(self, state: RunState) -> AgentResult:
        while True:
            if state.status == RunStatus.AWAITING_APPROVAL:
                return self._suspend(state)
            if state.status in {RunStatus.DONE, RunStatus.ERROR}:
                return self._finish(state)
            if state.step >= self.settings.max_steps:
                last = state.last_assistant()
                state.final_text = (last.content if last and last.content else "") or (
                    "I reached the maximum number of steps before finishing. Here is where I stopped."
                )
                state.status = RunStatus.DONE
                self._audit(state, "max_steps_reached", {"steps": state.step})
                return self._finish(state)

            state.messages[0] = Message.system(self._system_prompt(state))
            specs = self.tools.specs(state.allowed_tools)
            response = self.provider.generate(state.messages, tools=specs or None)
            state.step += 1
            state.add_usage(response.usage)
            self._audit(state, "model_call", {"step": state.step, "tool_calls": [c.name for c in response.tool_calls],
                                              "usage": response.usage, "text_preview": response.text[:200]})
            state.messages.append(response.to_message())

            if not response.tool_calls:
                if self._needs_revision(state, response.text):
                    continue
                state.final_text = response.text.strip()
                state.status = RunStatus.DONE
                continue

            outcome = self._execute_calls(state, start_index=0)
            if outcome == "deferred":
                return self._suspend(state)

    def _needs_revision(self, state: RunState, draft: str) -> bool:
        """Verification pass: ask the model to review its own final answer once."""
        if not self.settings.verify or state.verify_attempts >= 1 or not draft.strip():
            return False
        state.verify_attempts += 1
        checklist = "\n\n".join(self.skills.get(n).instructions[:3000] for n in state.loaded_skills) or "(none)"
        calls = ", ".join(f"{c.name}({'ok' if c.ok else 'error'})" for c in state.tool_calls) or "none"
        prompt = (
            "You are a strict reviewer of an AI agent's answer.\n\n"
            f"User request:\n{state.user_message}\n\n"
            f"Skill instructions the agent had to follow:\n{checklist}\n\n"
            f"Tools the agent called: {calls}\n\n"
            f"Draft answer:\n{draft}\n\n"
            "Check: were required steps skipped? are there claims not backed by tool results? "
            "is the answer in the user's language? is anything the user asked for missing?\n"
            'Reply with JSON only: {"ok": true|false, "issues": ["..."]}. '
            "Set ok=false only for real, actionable problems."
        )
        try:
            review = self.provider.generate([Message.user(prompt)], tools=None, temperature=0.0)
        except ProviderError as exc:
            log.warning("verification skipped: %s", exc)
            return False
        state.add_usage(review.usage)
        data = extract_json(review.text) or {}
        issues = [str(i) for i in (data.get("issues") or []) if str(i).strip()]
        ok = bool(data.get("ok", True)) or not issues
        self._audit(state, "verify", {"ok": ok, "issues": issues[:5]})
        if ok:
            return False
        state.messages.append(Message.user(
            "[Automated review of your draft answer found issues]\n- " + "\n- ".join(issues[:5]) +
            "\n\nFix them (call tools if needed) and then give the final answer."
        ))
        return True

    # ------------------------------------------------------------------
    # tool execution
    # ------------------------------------------------------------------
    def _context(self, state: RunState) -> ToolContext:
        def on_skill_loaded(name: str) -> None:
            self._load_skill(state, name)
            self._refresh_allowed_tools(state)

        return ToolContext(
            settings=self.settings, workspace=self.workspace, session_id=state.session_id,
            memory=self.memory, skills=self.skills, run_id=state.run_id, on_skill_loaded=on_skill_loaded,
        )

    def _execute_calls(self, state: RunState, *, start_index: int) -> str:
        """Execute the pending assistant tool calls from ``start_index``.

        Returns ``"continue"`` or ``"deferred"`` (run suspended for approval).
        """
        assistant = state.last_assistant()
        calls = assistant.tool_calls if assistant else []
        ctx = self._context(state)
        for index in range(start_index, len(calls)):
            call = calls[index]
            tool = self.tools.get(call.name)
            if tool is None or call.name not in state.allowed_tools:
                self._record_tool_error(state, call, f"tool '{call.name}' is not available in this turn. "
                                                     f"Available: {', '.join(state.allowed_tools)}")
                continue
            try:
                params = tool.validate(call.arguments)
            except ToolValidationError as exc:
                self._record_tool_error(state, call, str(exc))
                continue
            risk = tool.effective_risk(params, ctx)
            if risk == "confirm" and tool.name not in state.approved_tools and not self.settings.auto_approve:
                decision = self.approval_policy.decide(state, tool, call)
                self._audit(state, "approval_requested", {"tool": tool.name, "arguments": call.arguments,
                                                          "decision": decision.value})
                if decision == Decision.DEFER:
                    state.pending = PendingApproval(
                        approval_id=state.run_id, tool_name=tool.name, tool_call=call, risk=risk,
                        summary=self._summarise_call(call), description=tool.description,
                    )
                    state.pending_call_index = index
                    state.status = RunStatus.AWAITING_APPROVAL
                    return "deferred"
                if decision == Decision.DENY:
                    self._record_denied(state, call)
                    continue
                if decision == Decision.ALWAYS:
                    state.approved_tools.append(tool.name)
            self._run_tool(state, tool, call, ctx, approved=(risk == "confirm") or None)
        return "continue"

    def _apply_decision(self, state: RunState, decision: Decision) -> None:
        pending = state.pending
        if pending is None:
            return
        assistant = state.last_assistant()
        call = assistant.tool_calls[state.pending_call_index] if assistant else pending.tool_call
        state.pending = None
        state.status = RunStatus.RUNNING
        if decision == Decision.DENY:
            self._record_denied(state, call)
            return
        if decision == Decision.ALWAYS:
            state.approved_tools.append(pending.tool_name)
        tool = self.tools.get(pending.tool_name)
        if tool is None:
            self._record_tool_error(state, call, "tool disappeared before execution")
            return
        self._run_tool(state, tool, call, self._context(state), approved=True)

    def _run_tool(self, state: RunState, tool: Tool, call: ToolCall, ctx: ToolContext,
                  approved: Optional[bool]) -> None:
        self._audit(state, "tool_call", {"tool": tool.name, "arguments": call.arguments})
        result, ok, duration_ms = self.tools.execute(
            tool.name, call.arguments, ctx, max_chars=self.settings.tool_result_max_chars
        )
        state.messages.append(Message.tool_result(call, result))
        state.tool_calls.append(ToolCallRecord(name=tool.name, arguments=call.arguments, ok=ok,
                                               duration_ms=duration_ms, approved=approved,
                                               result_preview=result[:300]))
        self._audit(state, "tool_result", {"tool": tool.name, "ok": ok, "duration_ms": duration_ms,
                                           "preview": result[:300]})

    def _record_tool_error(self, state: RunState, call: ToolCall, message: str) -> None:
        payload = serialize_result({"error": message})
        state.messages.append(Message.tool_result(call, payload))
        state.tool_calls.append(ToolCallRecord(name=call.name, arguments=call.arguments, ok=False,
                                               result_preview=payload[:300]))
        self._audit(state, "tool_result", {"tool": call.name, "ok": False, "preview": message[:300]})

    def _record_denied(self, state: RunState, call: ToolCall) -> None:
        payload = serialize_result({
            "error": "denied", "detail": "The user denied this action. Do not retry it; explain and offer alternatives.",
        })
        state.messages.append(Message.tool_result(call, payload))
        state.tool_calls.append(ToolCallRecord(name=call.name, arguments=call.arguments, ok=False,
                                               approved=False, result_preview="denied by user"))
        self._audit(state, "tool_denied", {"tool": call.name, "arguments": call.arguments})

    @staticmethod
    def _summarise_call(call: ToolCall) -> str:
        args = json.dumps(call.arguments, ensure_ascii=False)
        if len(args) > 300:
            args = args[:300] + "…"
        return f"{call.name}({args})"

    # ------------------------------------------------------------------
    # termination helpers
    # ------------------------------------------------------------------
    def _suspend(self, state: RunState) -> AgentResult:
        self.memory.save_pending(state.run_id, state.session_id, state.model_dump(mode="json"))
        self._audit(state, "run_suspended", {"tool": state.pending.tool_name if state.pending else None})
        return AgentResult.from_state(state)

    def _finish(self, state: RunState) -> AgentResult:
        if state.status == RunStatus.DONE and state.final_text:
            self.memory.add_message(state.session_id, "assistant", state.final_text, {
                "run_id": state.run_id, "skills": state.loaded_skills,
                "tools": [c.name for c in state.tool_calls],
            })
        self._audit(state, "run_finished", {"status": state.status.value, "steps": state.step,
                                            "skills": state.loaded_skills, "usage": state.usage})
        return AgentResult.from_state(state)

    def _fail(self, state: RunState, message: str) -> AgentResult:
        state.status = RunStatus.ERROR
        state.error = message
        state.final_text = message
        self._audit(state, "run_failed", {"error": message})
        return AgentResult.from_state(state)

    def _audit(self, state: RunState, event: str, payload: Optional[dict[str, Any]] = None) -> None:
        try:
            self.memory.log(state.session_id, state.run_id, event, payload)
        except Exception:  # pragma: no cover - never let logging break a run
            log.exception("audit log failed")
