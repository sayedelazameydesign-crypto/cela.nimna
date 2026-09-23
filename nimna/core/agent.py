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
import fnmatch
import json
import logging
import time
import uuid
from typing import Any, Optional

# Sprint 1 infra: VisionCache (Redis + in-memory) and Anomaly Kill Switch (lazy imports for tests)
try:
    from nimna.vision.cache import get_vision_cache  # type: ignore
except Exception:  # pragma: no cover
    get_vision_cache = None  # type: ignore

try:
    from security.anomaly import get_detector  # type: ignore
except Exception:  # pragma: no cover
    get_detector = None  # type: ignore

# Swarm (Sprint 2) — lazy import to keep tests light
def _get_swarm_components():
    try:
        from .planner_swarm import PlannerSwarm
        from ..agents import AGENT_CLASSES
        return PlannerSwarm, AGENT_CLASSES
    except Exception:
        return None, None

from ..config import Settings
from ..evidence import EvidenceJournal
from ..governance import PolicyDecision, PolicyEngine
from ..mcp.naming import encode_name_pattern
from ..mcp.registry import APPROVED_KEY, MCP_TAG, attach_gateway
from ..memory.store import MemoryStore
from ..models import BudgetExceededError, CostGuard, GovernedModelProvider, ModelRegistry
from ..providers.base import Message, ModelProvider, ProviderError, ToolCall
from ..provenance.manifest import build_manifest
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
                 workspace: Optional[Any] = None, use_llm_planner: bool = True,
                 mcp_gateway: Optional[Any] = None):
        # Put the budget gate at the provider boundary so planner, verifier,
        # normal turns, and swarm calls share one policy.  The wrapper forwards
        # provider-specific attributes (for example MockProvider.calls).
        provider_info = provider.describe()
        self.model_registry = ModelRegistry.for_settings(settings, provider_info)
        if isinstance(provider, GovernedModelProvider):
            self.provider = provider
            self.cost_guard = provider.guard
        else:
            self.cost_guard = CostGuard.from_settings(settings, provider_info)
            self.provider = GovernedModelProvider(provider, self.cost_guard)
        self.policy = PolicyEngine()
        self.skills = skills
        self.tools = tools
        self.memory = memory
        self.settings = settings
        self.workspace = (workspace or settings.workspace_dir)
        self.approval_policy: ApprovalPolicy = approval_policy or (
            AutoApprove() if settings.auto_approve else DeferToClient()
        )
        self.selector = SkillSelector(self.provider, skills, max_skills=settings.max_skills,
                                      use_llm=use_llm_planner)
        # Transport for governed remote MCP tools. Held, not called: nothing
        # contacts an MCP server unless the registry contains a tool that
        # forwards to it, and that only happens when a skill asks for one.
        self.mcp_gateway = mcp_gateway
        self.evidence = EvidenceJournal(memory)
        self.runtime_manifest = build_manifest(
            settings=settings,
            provider=self.provider.describe(),
            skills=skills.list(),
            tools=tools.all(),
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def _should_swarm(self, user_message: str) -> bool:
        if not getattr(self.settings, "swarm_enabled", False):
            return False
        # explicit marker or env SWARM_FORCE
        if user_message.strip().startswith("[swarm]") or "swarm:" in user_message.lower():
            return True
        # heuristic: composite requests (Arabic و/ثم + multiple intents)
        low = user_message.lower()
        markers = [" و ", " ثم ", " بعد ", " and ", " then ", " وابحث", " ونفذ", " وابني"]
        composite = sum(1 for m in markers if m in low) >= 1 and len(user_message) > 80
        # also if provider is mock and tasks detected via keyword decompose -> still use swarm for demo
        if composite:
            return True
        # fallback: if planner_swarm would create >1 task, use swarm
        try:
            PlannerSwarm, _ = _get_swarm_components()
            if PlannerSwarm is not None:
                ps = PlannerSwarm(self.provider, self.settings)
                tasks = ps.decompose(user_message)
                if len(tasks) > 1:
                    return True
        except Exception:
            pass
        return False

    def run_swarm(self, user_message: str, session_id: Optional[str] = None) -> AgentResult:
        """Swarm orchestration: DAG -> parallel sub-agents -> synthesis."""
        import asyncio
        session_id = session_id or uuid.uuid4().hex[:12]
        self.memory.ensure_session(session_id)
        state = RunState(session_id=session_id, user_message=user_message, started_at=time.perf_counter())
        self._audit(state, "run_started", {"message": user_message[:500], "mode": "swarm",
                                             "runtime_fingerprint": self.runtime_manifest.get("sha256")})
        self.memory.add_message(session_id, "user", user_message, {"run_id": state.run_id, "mode": "swarm"})
        try:
            PlannerSwarm, _ = _get_swarm_components()
            if PlannerSwarm is None:
                raise RuntimeError("swarm components unavailable")
            planner = PlannerSwarm(self.provider, self.settings)
            tasks = planner.decompose(user_message)
            self._audit(state, "swarm_decomposed", {"tasks": [t.to_dict() for t in tasks]})
            # execute DAG
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                # unlikely in sync run, but handle via new loop in thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    fut = pool.submit(asyncio.run, planner.execute(tasks, session_id, self))
                    swarm_out = fut.result()
            else:
                swarm_out = asyncio.run(planner.execute(tasks, session_id, self))
            # map swarm tasks to tool_calls for UI
            for td in swarm_out.get("tasks", []):
                state.tool_calls.append(ToolCallRecord(name=f"swarm:{td['agent']}", arguments={"task": td["task"][:200]}, ok=(td.get("status")=="done"), result_preview=str(td.get("result",{}).get("output",""))[:300]))
            state.plan = [f"{t.id}:{t.agent}:{t.task[:60]}" for t in tasks]
            state.selection_reason = f"swarm DAG ({len(tasks)} tasks, batches={swarm_out.get('batches')})"
            state.final_text = swarm_out.get("synthesis", "") or "تم تنفيذ المهام عبر Swarm."
            state.status = RunStatus.DONE
            state.step = len(tasks)
            # persist synthesis
            self._audit(state, "swarm_finished", {"tasks": swarm_out.get("tasks"), "elapsed_ms": swarm_out.get("elapsed_ms"), "all_ok": swarm_out.get("all_ok")})
            return self._finish(state)
        except BudgetExceededError as exc:
            return self._fail(state, f"cost guard blocked the run: {exc}")
        except ProviderError as exc:
            return self._fail(state, f"model provider error: {exc}")
        except Exception as exc:
            log.exception("swarm run crashed")
            return self._fail(state, f"internal error: {type(exc).__name__}: {exc}")

    def run(self, user_message: str, session_id: Optional[str] = None) -> AgentResult:
        # Swarm fast-path (if enabled and request is composite)
        try:
            if self._should_swarm(user_message):
                return self.run_swarm(user_message, session_id=session_id)
        except Exception as exc:
            log.warning("swarm check failed (%s), falling back to single-agent", exc)
        if len(user_message) > self.settings.max_user_message_chars:
            # truncate rather than reject – keep UX friendly but bounded
            user_message = user_message[: self.settings.max_user_message_chars] + "\n[... message truncated]"
        session_id = session_id or uuid.uuid4().hex[:12]
        self.memory.ensure_session(session_id)
        state = RunState(session_id=session_id, user_message=user_message, started_at=time.perf_counter())
        self._audit(state, "run_started", {"message": user_message[:500],
                                             "runtime_fingerprint": self.runtime_manifest.get("sha256")})
        self.memory.add_message(session_id, "user", user_message, {"run_id": state.run_id})
        try:
            self._prepare(state)
            return self._drive(state)
        except BudgetExceededError as exc:
            return self._fail(state, f"cost guard blocked the run: {exc}")
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
        # restore perf counter baseline after deserialization
        if not state.started_at:
            state.started_at = time.perf_counter()
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
        except BudgetExceededError as exc:
            return self._fail(state, f"cost guard blocked the run: {exc}")
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
                    for resolved in self._resolve_tool_entry(skill_name, tool_name):
                        if resolved not in allowed:
                            allowed.append(resolved)
        else:
            for tool_name in self.settings.default_tools:
                if tool_name in self.tools and tool_name not in allowed:
                    allowed.append(tool_name)
        state.allowed_tools = allowed

    def _resolve_tool_entry(self, skill_name: str, entry: str) -> list[str]:
        """Resolve one skill ``allowed_tools`` entry to concrete tool names.

        A plain name resolves to itself. An entry containing ``*`` or ``?`` is
        a glob, matched against the registry — which is what lets a skill grant
        tools whose names are only known at runtime. Remote MCP tools are named
        ``mcp__{server}__{tool}`` after whatever servers the operator enabled,
        so a ``SKILL.md`` cannot list them literally; it declares the shape
        (``mcp__*__*``) and the gate stays here, in the registry match, rather
        than becoming documentation-only.

        A row that matches nothing is a warning, not a silent no-op: a skill
        that grants nothing is usually a mistake, and a remote server that
        failed to register would otherwise look like a skill with no tools.

        The entry is escaped the same way tool names are, so an Arabic pattern
        such as ``mcp__*__طقس*`` matches the Arabic-named tool it refers to.
        For ASCII entries this is the identity, so nothing else changes.
        """
        try:
            pattern = encode_name_pattern(entry)
        except ValueError:
            log.warning("skill %s has an empty allowed_tools entry", skill_name)
            return []

        if any(char in entry for char in "*?["):
            matched = [
                name for name in sorted(self.tools.names()) if fnmatch.fnmatchcase(name, pattern)
            ]
            if not matched:
                log.warning(
                    "skill %s pattern %r matched no registered tool", skill_name, entry
                )
            return matched
        if pattern in self.tools:
            return [pattern]
        log.warning("skill %s references unknown tool %s", skill_name, entry)
        return []

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
        if not state.started_at:
            state.started_at = time.perf_counter()
        while True:
            # runtime guard
            elapsed = time.perf_counter() - state.started_at
            if elapsed > self.settings.max_runtime_seconds:
                state.final_text = (
                    f"توقف التنفيذ بعد {elapsed:.0f} ثانية لتجاوز الحد الأقصى للوقت "
                    f"({self.settings.max_runtime_seconds} ثانية). "
                    f"تم تنفيذ {state.tool_call_count} أداة في {state.step} خطوة. حاول تقسيم المهمة."
                )
                state.status = RunStatus.DONE
                self._audit(state, "max_runtime_reached", {"elapsed": elapsed, "steps": state.step})
                return self._finish(state)
            if state.status == RunStatus.AWAITING_APPROVAL:
                return self._suspend(state)
            if state.status in {RunStatus.DONE, RunStatus.ERROR}:
                return self._finish(state)
            if state.step >= self.settings.max_steps:
                last = state.last_assistant()
                state.final_text = (last.content if last and last.content else "") or (
                    "Reached the maximum number of steps before finishing. Here is where I stopped."
                )
                state.status = RunStatus.DONE
                self._audit(state, "max_steps_reached", {"steps": state.step})
                return self._finish(state)
            if state.tool_call_count >= self.settings.max_tool_calls:
                state.final_text = (
                    f"توقفت بعد {state.tool_call_count} استدعاء أداة (الحد {self.settings.max_tool_calls}). "
                    "الرجاء تبسيط الطلب أو تجزئته."
                )
                state.status = RunStatus.DONE
                self._audit(state, "max_tool_calls_reached", {"tool_calls": state.tool_call_count})
                return self._finish(state)
            if state.consecutive_failures >= self.settings.max_consecutive_failures:
                state.final_text = (
                    f"توقفت بعد {state.consecutive_failures} أخطاء متتالية للأدوات. راجع المدخلات وحاول مرة أخرى."
                )
                state.status = RunStatus.DONE
                self._audit(state, "max_failures_reached", {"failures": state.consecutive_failures})
                return self._finish(state)

            state.messages[0] = Message.system(self._system_prompt(state))
            specs = self.tools.specs(state.allowed_tools)
            try:
                response = self.provider.generate(
                    state.messages, tools=specs or None,
                    max_tokens=self.settings.max_response_tokens,
                )
            except TypeError:
                # mock provider in tests may not accept max_tokens yet
                response = self.provider.generate(state.messages, tools=specs or None)
            state.step += 1
            state.add_usage(response.usage)
            self._audit(state, "model_call", {"step": state.step, "tool_calls": [c.name for c in response.tool_calls],
                                              "usage": response.usage, "cost_guard": self.cost_guard.status(),
                                              "text_preview": response.text[:200]})
            # repetition guard on plain text answers
            if not response.tool_calls:
                text_norm = response.text.strip()
                if text_norm and text_norm == state.last_text:
                    state.repeat_text_count += 1
                else:
                    state.repeat_text_count = 0
                state.last_text = text_norm
                if state.repeat_text_count >= 2:
                    state.final_text = text_norm or "Model repeated the same answer; stopping."
                    state.status = RunStatus.DONE
                    self._audit(state, "loop_detected", {"reason": "repeated_text"})
                    return self._finish(state)

            state.messages.append(response.to_message())

            if not response.tool_calls:
                if self._needs_revision(state, response.text):
                    continue
                state.final_text = response.text.strip()
                state.status = RunStatus.DONE
                # success resets failure streak
                state.consecutive_failures = 0
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
            try:
                review = self.provider.generate([Message.user(prompt)], tools=None, temperature=0.0,
                                                max_tokens=800)
            except TypeError:
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
            # The approval ledger is a fresh mutable set per execution pass: a
            # remote tool is only callable if *this* pass cleared it.
            extras=attach_gateway(self.mcp_gateway, approved=set()),
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
            # loop guards before each call
            if state.tool_call_count >= self.settings.max_tool_calls:
                self._record_tool_error(state, call, f"tool call limit {self.settings.max_tool_calls} reached – not executing {call.name}")
                state.consecutive_failures += 1
                continue
            sig = self._signature(call)
            if sig in state.seen_signatures:
                # allow one repeat for retry, stop on third identical call
                occurrences = state.seen_signatures.count(sig)
                if occurrences >= 2:
                    self._audit(state, "loop_detected", {"tool": call.name, "signature": sig})
                    self._record_tool_error(state, call, f"repeated call to '{call.name}' with identical arguments – loop detected, stopping this branch")
                    state.consecutive_failures += 1
                    continue
            state.seen_signatures.append(sig)
            # cap history length for JSON stability
            if len(state.seen_signatures) > 100:
                state.seen_signatures = state.seen_signatures[-60:]

            tool = self.tools.get(call.name)
            if tool is None or call.name not in state.allowed_tools:
                self._record_tool_error(state, call, f"tool '{call.name}' is not available in this turn. "
                                                     f"Available: {', '.join(state.allowed_tools)}")
                state.consecutive_failures += 1
                continue
            try:
                params = tool.validate(call.arguments)
            except ToolValidationError as exc:
                self._record_tool_error(state, call, str(exc))
                state.consecutive_failures += 1
                continue
            risk = tool.effective_risk(params, ctx)
            # A restricted skill raises even read-only tools to the approval
            # tier; the skill boundary must not be a documentation-only label.
            if risk == "safe" and any(
                self.skills.get(skill_name).meta.risk_level == "restricted"
                for skill_name in state.loaded_skills
            ):
                risk = "confirm"
            # A remote tool must never run unapproved, whatever its registration
            # says. Its handler asserts consent on the gateway's behalf on the
            # basis that the agent already approved this call, so a remote tool
            # registered as `safe` would turn that assertion into a bypass.
            # Enforced here, at the one place that decides to execute.
            if risk == "safe" and MCP_TAG in tool.tags:
                risk = "confirm"
            # Governance is checked after scope/validation but before any side
            # effect.  The legacy approval policy remains the user-facing gate.
            approval_key = self._approval_key(tool.name, state)
            legacy_approved = tool.name in state.approved_tools
            scoped_approved = approval_key in state.approved_tools
            policy_result = self.policy.evaluate(
                tool_name=tool.name,
                declared_risk=risk,
                allowed_tools=state.allowed_tools,
                explicit_consent=(scoped_approved or legacy_approved or self.settings.auto_approve),
                contains_secret=("secret" in tool.tags),
            )
            if policy_result.decision is PolicyDecision.DENY:
                self._record_tool_error(state, call, f"policy denied '{tool.name}': {policy_result.reason}")
                state.consecutive_failures += 1
                continue
            if policy_result.decision is PolicyDecision.APPROVAL_REQUIRED and not scoped_approved and not legacy_approved and not self.settings.auto_approve:
                decision = self.approval_policy.decide(state, tool, call)
                self._audit(state, "approval_requested", {"tool": tool.name, "arguments": call.arguments,
                                                          "decision": decision.value, "approval_key": approval_key})
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
                    state.consecutive_failures += 1
                    continue
                if decision == Decision.ALWAYS:
                    # store both scoped and legacy for backward compat
                    if approval_key not in state.approved_tools:
                        state.approved_tools.append(approval_key)
                    if tool.name not in state.approved_tools:
                        state.approved_tools.append(tool.name)
            # approved or safe – run it
            self._run_tool(state, tool, call, ctx, approved=(risk != "safe") or None)
            # update counters
            state.tool_call_count += 1
            last_record = state.tool_calls[-1] if state.tool_calls else None
            if last_record and last_record.ok:
                state.consecutive_failures = 0
            else:
                state.consecutive_failures += 1
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
            state.consecutive_failures += 1
            return
        if decision == Decision.ALWAYS:
            key = self._approval_key(pending.tool_name, state)
            if key not in state.approved_tools:
                state.approved_tools.append(key)
            if pending.tool_name not in state.approved_tools:
                state.approved_tools.append(pending.tool_name)
        tool = self.tools.get(pending.tool_name)
        if tool is None:
            self._record_tool_error(state, call, "tool disappeared before execution")
            state.consecutive_failures += 1
            return
        self._run_tool(state, tool, call, self._context(state), approved=True)
        state.tool_call_count += 1
        state.seen_signatures.append(self._signature(call))
        last_record = state.tool_calls[-1] if state.tool_calls else None
        if last_record and last_record.ok:
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1

    def _run_tool(self, state: RunState, tool: Tool, call: ToolCall, ctx: ToolContext,
                  approved: Optional[bool]) -> None:
        self._audit(state, "tool_call", {"tool": tool.name, "arguments": call.arguments})
        # Evidence for the remote handler. `_run_tool` is the one funnel every
        # execution passes through — the approval path and the resume path both
        # land here — so a remote tool is recorded as cleared exactly when it is
        # about to run. The handler refuses unless it finds the tool here, which
        # is what makes its `explicit_consent=True` evidenced rather than
        # asserted.
        if MCP_TAG in tool.tags:
            ctx.extras.setdefault(APPROVED_KEY, set()).add(tool.name)
        result, ok, duration_ms = self.tools.execute(
            tool.name, call.arguments, ctx, max_chars=self.settings.tool_result_max_chars
        )
        state.messages.append(Message.tool_result(call, result))
        state.tool_calls.append(ToolCallRecord(name=tool.name, arguments=call.arguments, ok=ok,
                                               duration_ms=duration_ms, approved=approved,
                                               result_preview=result[:300]))
        self._audit(state, "tool_result", {"tool": tool.name, "ok": ok, "duration_ms": duration_ms,
                                           "preview": result[:300]})
        # -- Heuristics Kill Switch (security/anomaly.py) — real-time ---
        if tool.name == "shell_execute" and get_detector is not None:
            try:
                _det = get_detector()
                _det.log_call(state.session_id, tool.name, dict(call.arguments), stdout=result[:1000] if ok else "", stderr="" if ok else result[:1000])
                _kill, _reason = _det.should_kill(state.session_id, recent_logs=result[:2000] if isinstance(result, str) else str(call.arguments)[:1000])
                if _kill:
                    self._audit(state, "anomaly_kill", {"reason": _reason, "tool": tool.name})
                    state.messages.append(Message.user(f"[Kill Switch] تم إيقاف الإجراء المشبوه: {_reason} — تم حظر الجلسة تلقائيا." ))
                    state.final_text = f"تم إيقاف الجلسة تلقائيا بواسطة نظام الحماية (Heuristics Kill Switch): {_reason}. يرجى مراجعة السجل وتقسيم المهمة إلى خطوات آمنة."
                    state.status = RunStatus.DONE
                    return
            except Exception:
                pass
        # Vision gateway: after a successful screenshot, inject the image so the
        # next model turn sees the desktop (observe → plan → act loop).
        if tool.name == "take_screenshot" and ok:
            try:
                import base64, json
                data = json.loads(result) if result else {}
                # unwrap truncation wrapper if needed
                if data.get("truncated") and isinstance(data.get("result"), str):
                    try:
                        inner = json.loads(data["result"])
                        if isinstance(inner, dict):
                            data = inner
                    except Exception:
                        pass
                # the tool returns {"path": " .screenshots/...", ...} — path is display_path
                rel = data.get("path") or ""
                # resolve inside workspace
                if rel:
                    # display_path is relative like ".screenshots/xxx.png" or "reports/..."
                    p = (ctx.workspace / rel).resolve()
                    # ensure still inside workspace
                    try:
                        p.relative_to(ctx.workspace.resolve())
                    except ValueError:
                        p = None
                    if p and p.is_file():
                        raw = p.read_bytes()
                        # limit to 1.5MB for model (downscale if needed — here just truncate)
                        if len(raw) > 1_500_000:
                            raw = raw[:1_500_000]
                        _w = int(data.get("width") or 0)
                        _h = int(data.get("height") or 0)
                        # --- VisionCache lookup (Redis + fallback, 600s) ---
                        _cached = None
                        _vc = None
                        if get_vision_cache is not None:
                            try:
                                _vc = get_vision_cache()
                                _cached = _vc.get(raw, _w, _h)
                            except Exception:
                                _cached = None
                        if _cached is not None and isinstance(_cached, dict) and "b64" in _cached:
                            # cache hit — reuse encoded payload
                            b64 = _cached["b64"]
                            mime = _cached.get("mime", "image/png")
                            caption = _cached.get("caption", f"[Screenshot: {rel} — cached]")
                            h = _cached.get("hash") or ""
                            if h:
                                state.screenshot_hashes.append(h)
                                if len(state.screenshot_hashes) > 10:
                                    state.screenshot_hashes = state.screenshot_hashes[-10:]
                                # consecutive identical logic for cached path too
                                if len(state.screenshot_hashes) >= 3 and len(set(state.screenshot_hashes[-3:])) == 1:
                                    state.consecutive_identical_screenshots = state.consecutive_identical_screenshots + 1 if state.consecutive_identical_screenshots else 3
                                else:
                                    cnt = 1
                                    for i in range(len(state.screenshot_hashes)-1, 0, -1):
                                        if state.screenshot_hashes[i] == state.screenshot_hashes[i-1]:
                                            cnt += 1
                                        else:
                                            break
                                    state.consecutive_identical_screenshots = cnt if cnt > 1 else 0
                                self._audit(state, "vision_hash", {"hash": h, "consecutive": state.consecutive_identical_screenshots, "cached": True})
                            state.messages.append(Message.user_with_image(caption, b64, mime))
                            self._audit(state, "vision_cache_hit", {"path": rel, "bytes": len(raw), "hash": h})
                            self._audit(state, "vision_injected", {"path": rel, "bytes": len(raw), "mime": mime, "hash": state.screenshot_hashes[-1] if state.screenshot_hashes else None, "cached": True})
                        else:
                            b64 = base64.b64encode(raw).decode("ascii")
                            mime = "image/jpeg" if p.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                            # --- visual duplicate detector (break screenshot loops) ---
                            try:
                                import hashlib
                                # lightweight perceptual hash: 16x16 grayscale
                                try:
                                    from PIL import Image
                                    import io
                                    img = Image.open(io.BytesIO(raw)).convert("L").resize((16,16))
                                    h = hashlib.md5(img.tobytes()).hexdigest()[:12]
                                except Exception:
                                    h = hashlib.md5(raw[:4096]).hexdigest()[:12]
                                state.screenshot_hashes.append(h)
                                # keep last 10
                                if len(state.screenshot_hashes) > 10:
                                    state.screenshot_hashes = state.screenshot_hashes[-10:]
                                # check last 3 identical
                                if len(state.screenshot_hashes) >= 3 and len(set(state.screenshot_hashes[-3:])) == 1:
                                    state.consecutive_identical_screenshots = state.consecutive_identical_screenshots + 1 if state.consecutive_identical_screenshots else 3
                                else:
                                    # count consecutive identical from tail
                                    cnt = 1
                                    for i in range(len(state.screenshot_hashes)-1, 0, -1):
                                        if state.screenshot_hashes[i] == state.screenshot_hashes[i-1]:
                                            cnt += 1
                                        else:
                                            break
                                    state.consecutive_identical_screenshots = cnt if cnt > 1 else 0
                                # audit
                                self._audit(state, "vision_hash", {"hash": h, "consecutive": state.consecutive_identical_screenshots})
                                # if 3 identical, inject warning for the model
                                if state.consecutive_identical_screenshots >= 3:
                                    warn = (
                                        "تنبيه: الشاشة لم تتغير منذ 3 محاولات متتالية (hash=%s). "
                                        "حاول تغيير الاستراتيجية: استخدم shell_execute للتحقق من العمليات الخلفية، "
                                        "أو get_element_coordinates للعثور على العنصر بدقة، أو قم بالتمرير/فتح قائمة مختلفة."
                                    ) % h
                                    state.messages.append(Message.user(warn))
                                    self._audit(state, "screenshot_loop_detected", {"hash": h, "count": state.consecutive_identical_screenshots})
                                    # reset to avoid spamming every turn (will trigger again if still identical)
                                    state.consecutive_identical_screenshots = 0
                            except Exception:
                                h = ""
                                pass
                            # inject as a user message with image + caption
                            caption = f"[Screenshot: {rel} — {data.get('source','')} — {data.get('width','')}x{data.get('height','')}]"
                            state.messages.append(Message.user_with_image(caption, b64, mime))
                            self._audit(state, "vision_injected", {"path": rel, "bytes": len(raw), "mime": mime, "hash": state.screenshot_hashes[-1] if state.screenshot_hashes else None})
                            # populate cache for next time
                            if _vc is not None:
                                try:
                                    _vc.set(raw, _w, _h, {"b64": b64, "mime": mime, "caption": caption, "hash": state.screenshot_hashes[-1] if state.screenshot_hashes else h})
                                except Exception:
                                    pass
            except Exception:
                # never break the run on vision failure
                pass
        # also inject annotated screenshot from get_element_coordinates
        if tool.name == "get_element_coordinates" and ok:
            try:
                import base64 as _b64, json as _json
                _data = _json.loads(result) if result else {}
                if _data.get("truncated") and isinstance(_data.get("result"), str):
                    try:
                        _inner = _json.loads(_data["result"])
                        if isinstance(_inner, dict):
                            _data = _inner
                    except Exception:
                        pass
                _rel = _data.get("annotated_screenshot") or ""
                if _rel:
                    _p = (ctx.workspace / _rel).resolve()
                    try:
                        _p.relative_to(ctx.workspace.resolve())
                    except ValueError:
                        _p = None
                    if _p and _p.is_file():
                        _raw = _p.read_bytes()
                        if len(_raw) > 1_500_000:
                            _raw = _raw[:1_500_000]
                        _b64s = _b64.b64encode(_raw).decode("ascii")
                        _mime = "image/png"
                        state.messages.append(Message.user_with_image(f"[Locate: {_data.get('element','')} at ({_data.get('x')},{_data.get('y')})]", _b64s, _mime))
                        self._audit(state, "vision_injected", {"path": _rel, "bytes": len(_raw), "mime": _mime, "kind": "locate"})
            except Exception:
                pass

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

    @staticmethod
    def _signature(call: ToolCall) -> str:
        return f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"

    def _approval_key(self, tool_name: str, state: RunState) -> str:
        """Scoped permanent-approval key: tool + skill name:version.

        ``ALWAYS`` should not auto-approve the same tool for a different
        skill set, so the key binds the decision to the exact skill versions
        that were active when it was granted.
        """
        parts: list[str] = []
        for sname in sorted(state.loaded_skills):
            try:
                ver = self.skills.get(sname).meta.version
            except Exception:
                ver = "?"
            parts.append(f"{sname}:{ver}")
        # for core tools that are not skill-bound, no skill suffix – tool alone
        if not parts:
            return tool_name
        return f"{tool_name}|" + ",".join(parts)

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
        elapsed_ms = int(max(0.0, time.perf_counter() - state.started_at) * 1000) if state.started_at else 0
        self._audit(state, "run_finished", {"status": state.status.value, "steps": state.step,
                                            "skills": state.loaded_skills, "usage": state.usage,
                                            "elapsed_ms": elapsed_ms})
        return AgentResult.from_state(state)

    def _fail(self, state: RunState, message: str) -> AgentResult:
        state.status = RunStatus.ERROR
        state.error = message
        state.final_text = message
        self._audit(state, "run_failed", {"error": message})
        return AgentResult.from_state(state)

    def _audit(self, state: RunState, event: str, payload: Optional[dict[str, Any]] = None) -> None:
        try:
            self.evidence.record(state.session_id, state.run_id, event, payload)
        except Exception:  # pragma: no cover - never let logging break a run
            log.exception("audit log failed")
