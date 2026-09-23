"""PlannerSwarm — DAG decomposition + parallel execution + aggregation.

Flow:
  user_message -> decompose() -> [SwarmTask] (DAG) -> batches (topo) -> asyncio.gather -> synthesize

Design choices:
- LLM decomposition with strict JSON schema; fallback keyword heuristics (works offline / mock).
- Parallelism via asyncio.gather + run_in_executor (provider.generate is sync).
- Vector memory (execution_history / code_knowledge) is available to sub-agents transparently.
- Final synthesis via LLM again (or concatenation fallback).

Multilingual: prompts handle Arabic + English.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import Settings
from ..providers.base import Message

log = logging.getLogger(__name__)

DECOMPOSE_PROMPT = """You are the Swarm Planner for Nimna. Break a complex user request into a DAG of subtasks for specialized agents.

Agents:
- search: web/knowledge retrieval (web_search, fetch_url, vector_memory_search)
- code: write/execute/fix code (shell_execute, run_python, write_file) — has Self-Healing
- vision: visual desktop (take_screenshot, get_element_coordinates, mouse_click, type_text)

Rules:
- Use 1-3 tasks max. Each task must be independent or depend on previous via depends_on.
- Parallel tasks share no dependency and will run concurrently (up to 50% latency saving).
- Prefer splitting: research || implementation || verification.
- Reply JSON only (no prose, no fences): {{"tasks": [{{"id":"t1","agent":"search|code|vision","task":"...","depends_on":[]}}]}}

User request: {request}

Lexical hint: {hint}
"""

SYNTHESIZE_PROMPT = """You are the Swarm Aggregator. Merge sub-agent outputs into a final answer for the user.

User request: {request}

Sub-agent results:
{results}

Rules:
- Answer in user's language (Arabic if Arabic).
- Cite which agent did what (search/code/vision).
- If any agent failed, explain and offer fix.
- Be concise but complete. Tables for data, file paths for artifacts.
"""


@dataclass
class SwarmTask:
    id: str
    agent: str  # search | code | vision
    task: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    result: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "agent": self.agent, "task": self.task, "depends_on": self.depends_on, "status": self.status, "result": self.result}


def _keyword_decompose(request: str, max_agents: int = 3) -> list[SwarmTask]:
    """Offline fallback — keyword heuristics (Arabic + English)."""
    req_low = request.lower()
    tasks: list[SwarmTask] = []
    tid = 1

    def add(agent: str, desc: str, depends: list[str] | None = None):
        nonlocal tid
        if len(tasks) >= max_agents:
            return
        tasks.append(SwarmTask(id=f"t{tid}", agent=agent, task=desc, depends_on=depends or []))
        tid += 1

    # signals
    needs_search = any(k in req_low for k in ["ابحث", "بحث", "search", "fetch", "استخرج", "المصادر", "sources", "web"])
    needs_code = any(k in req_low for k in ["كود", "code", "python", "اكتب", "نفذ", "script", "csv", "تحليل", "analyze", "report"])
    needs_vision = any(k in req_low for k in ["صورة", "screenshot", "شاشة", "واجهة", "انقر", "click", "desktop", "vnc", "متصفح"])

    # if ambiguous / composite, split
    if needs_search and needs_code:
        add("search", f"اجمع المعلومات المطلوبة لـ: {request[:300]}")
        add("code", f"اكتب/نفذ الكود المطلوب لـ: {request[:300]}", depends=["t1"])
        if needs_vision and max_agents >= 3:
            add("vision", f"تحقق بصرياً من النتيجة لـ: {request[:300]}", depends=["t2"])
    elif needs_search:
        add("search", request[:500])
    elif needs_code:
        add("code", request[:500])
    elif needs_vision:
        add("vision", request[:500])
    else:
        # default: single code/search depending on verbs
        if any(k in req_low for k in ["حلل", "أنشئ", "اكتب"]):
            add("code", request[:500])
        else:
            add("search", request[:500])

    if not tasks:
        tasks.append(SwarmTask(id="t1", agent="code", task=request[:500]))
    return tasks


class PlannerSwarm:
    def __init__(self, provider, settings: Settings):
        self.provider = provider
        self.settings = settings

    def decompose(self, request: str) -> list[SwarmTask]:
        max_agents = max(1, min(self.settings.swarm_max_agents, 3))
        hint = f"search={any(k in request.lower() for k in ['بحث','search'])} code={any(k in request.lower() for k in ['كود','code','python'])} vision={any(k in request.lower() for k in ['شاشة','vision','click'])}"

        # try LLM if provider available (works for mock too if it implements planner prompt)
        try:
            prompt = DECOMPOSE_PROMPT.format(request=request[:1500], hint=hint)
            resp = self.provider.generate([Message.user(prompt)], tools=None, temperature=0.0, max_tokens=800)
            data = _extract_json(resp.text)
            if data and isinstance(data.get("tasks"), list) and 1 <= len(data["tasks"]) <= 3:
                tasks: list[SwarmTask] = []
                seen = set()
                for idx, raw in enumerate(data["tasks"][:max_agents], 1):
                    agent = str(raw.get("agent", "")).lower().strip()
                    if agent not in ("search", "code", "vision"):
                        continue
                    task_desc = str(raw.get("task", "")).strip()[:600] or request[:400]
                    tid = str(raw.get("id", f"t{idx}")).strip() or f"t{idx}"
                    if tid in seen:
                        tid = f"t{idx}"
                    seen.add(tid)
                    depends = raw.get("depends_on") or []
                    if not isinstance(depends, list):
                        depends = []
                    depends = [str(d).strip() for d in depends if str(d).strip() in seen]
                    tasks.append(SwarmTask(id=tid, agent=agent, task=task_desc, depends_on=depends))
                if tasks:
                    return tasks
        except Exception as exc:
            log.debug("swarm decompose LLM failed (%s) -> fallback", exc)

        return _keyword_decompose(request, max_agents=max_agents)

    def _batches(self, tasks: list[SwarmTask]) -> list[list[SwarmTask]]:
        """Topological batches for parallel execution."""
        # simple Kahn
        id_map = {t.id: t for t in tasks}
        indeg = {t.id: len(t.depends_on) for t in tasks}
        batches: list[list[SwarmTask]] = []
        remaining = set(id_map.keys())
        while remaining:
            ready = [id_map[i] for i in remaining if indeg[i] == 0]
            if not ready:
                # cycle → break with remaining as one batch
                ready = [id_map[i] for i in remaining]
            batches.append(ready)
            for t in ready:
                remaining.remove(t.id)
                for other in tasks:
                    if t.id in other.depends_on:
                        indeg[other.id] -= 1
        return batches

    async def execute(
        self,
        tasks: list[SwarmTask],
        session_id: str,
        parent_agent,  # Agent instance for provider/memory/tools/workspace
    ) -> dict[str, Any]:
        """Execute DAG in parallel batches. Returns {tasks: [...], synthesis: str}."""
        from ..agents import AGENT_CLASSES

        batches = self._batches(tasks)
        shared: dict[str, Any] = {}
        all_results: dict[str, Any] = {}
        started = time.perf_counter()

        for batch in batches:
            # build coros for this batch (parallel)
            coros = []
            for t in batch:
                # inject shared context from previous batches
                ctx = {"shared_results": dict(shared)}
                AgentCls = AGENT_CLASSES.get(t.agent)
                if AgentCls is None:
                    t.status = "failed"
                    t.result = {"error": f"unknown agent {t.agent}"}
                    continue
                agent = AgentCls(
                    provider=parent_agent.provider,
                    settings=parent_agent.settings,
                    memory=parent_agent.memory,
                    tools=parent_agent.tools,
                    workspace=parent_agent.workspace,
                    session_id=session_id,
                )
                # annotate for orchestrator visibility
                t.status = "running"
                if self.settings.swarm_parallel:
                    coros.append((t, agent.arun(t.task, session_id=session_id, context=ctx)))
                else:
                    # sequential
                    coro = agent.arun(t.task, session_id=session_id, context=ctx)
                    coros.append((t, coro))

            if coros:
                # run simultaneously
                if self.settings.swarm_parallel:
                    # gather
                    results = await asyncio.gather(*[c for _, c in coros], return_exceptions=True)
                    for (t, _), r in zip(coros, results):
                        if isinstance(r, Exception):
                            t.status = "failed"
                            t.result = {"error": str(r)[:500], "agent": t.agent}
                            all_results[t.id] = t.result
                            shared[t.id] = f"FAILED: {r}"
                        else:
                            # r is SwarmResult
                            t.status = "done" if r.ok else "failed"
                            t.result = r.to_dict()
                            all_results[t.id] = t.result
                            shared[t.id] = r.output[:800]
                else:
                    # sequential
                    for t, coro in coros:
                        try:
                            r = await coro
                            t.status = "done" if r.ok else "failed"
                            t.result = r.to_dict()
                            all_results[t.id] = t.result
                            shared[t.id] = r.output[:800]
                        except Exception as e:
                            t.status = "failed"
                            t.result = {"error": str(e)[:500], "agent": t.agent}
                            all_results[t.id] = t.result
                            shared[t.id] = f"FAILED: {e}"

                # audit batch
                try:
                    parent_agent.memory.log(session_id, f"swarm-{uuid.uuid4().hex[:8]}", "swarm_batch", {"batch": [t.id for t in batch], "status": {t.id: t.status for t in batch}})
                except Exception:
                    pass

        # synthesis via LLM (or concat fallback)
        synthesis = await self._synthesize(tasks, shared, parent_agent, session_id)

        elapsed = int((time.perf_counter() - started) * 1000)
        return {
            "tasks": [t.to_dict() for t in tasks],
            "batches": [[t.id for t in b] for b in batches],
            "shared": shared,
            "synthesis": synthesis,
            "elapsed_ms": elapsed,
            "all_ok": all(t.status == "done" for t in tasks),
        }

    async def _synthesize(self, tasks: list[SwarmTask], shared: dict[str, Any], parent_agent, session_id: str) -> str:
        # Use provider to merge (try LLM first; fallback to concat)
        try:
            # build results text
            results_txt = "\n".join(f"[{t.agent} {t.id}] {t.task[:200]}\n-> { (t.result or {}).get('output','')[:800]}" for t in tasks)
            prompt = SYNTHESIZE_PROMPT.format(request=tasks[0].task[:500] if tasks else "", results=results_txt[:3000])
            # provider.generate is sync → to_thread
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: self.provider.generate([Message.user(prompt)], tools=None, temperature=0.2, max_tokens=800))
            text = (resp.text or "").strip()[:4000]
            # if mock provider returns empty or planner-like fallback indicator, use concat
            if not text or text.startswith("{"):
                raise RuntimeError("empty synthesis")
            return text or "تمت معالجة المهام بنجاح."
        except Exception:
            # fallback: concatenate
            parts = []
            for t in tasks:
                out = (t.result or {}).get("output", "") if t.result else ""
                status = t.status
                parts.append(f"**{t.agent} ({t.id}) [{status}]**: {out[:600]}")
            return "\n\n".join(parts) or "تم التنفيذ."


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    import json, re

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    cands = [fenced.group(1)] if fenced else []
    cands.append(text)
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e > s:
        cands.append(text[s : e + 1])
    for c in cands:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None
