"""Skill selection (planner step).

1. Cheap lexical ranking over triggers/description (works offline).
2. LLM picks up to ``max_skills`` from the compact catalog and returns JSON.
3. Names are validated; on unusable output we fall back to the lexical ranking.
"""
import json
import logging
import re

from pydantic import BaseModel, Field

from ..providers.base import Message, ModelProvider, ProviderError
from ..skills.manager import SkillManager

log = logging.getLogger(__name__)

SELECTION_PROMPT = """You are the skill router of an AI agent.
Given the user's request and the catalog of installed skills (name + description + triggers),
choose which skills (0 to {max_skills}) the agent should load to do the job well.

Rules:
- Pick a skill only if its description clearly matches the request.
- A request may need several skills (e.g. analysing data AND writing a report).
- Return NO skills for small talk or questions answerable without tools.
- Reply with JSON only, no prose:
{{"skills": ["skill_name", ...], "reason": "one short sentence", "plan": ["step 1", "step 2"]}}

Skill catalog:
{catalog}

Recent conversation (may be empty):
{history}

Lexical pre-ranking (hint only): {hint}

User request:
{request}
"""


class SkillSelection(BaseModel):
    skills: list[str] = Field(default_factory=list)
    reason: str = ""
    plan: list[str] = Field(default_factory=list)
    source: str = "llm"  # llm | keywords | none


def extract_json(text: str) -> dict | None:
    """Tolerant JSON extraction (handles ```json fences and surrounding prose)."""
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


class SkillSelector:
    def __init__(self, provider: ModelProvider, skills: SkillManager, *, max_skills: int = 3,
                 use_llm: bool = True):
        self.provider = provider
        self.skills = skills
        self.max_skills = max_skills
        self.use_llm = use_llm

    def select(self, request: str, history: list[Message] | None = None) -> SkillSelection:
        if len(self.skills) == 0:
            return SkillSelection(source="none", reason="no skills installed")

        ranked = self.skills.rank_by_keywords(request, limit=self.max_skills + 2)
        hint = ", ".join(f"{meta.name}({score})" for meta, score in ranked) or "none"

        if self.use_llm:
            history_text = "\n".join(
                f"{m.role}: {m.content[:300]}" for m in (history or [])[-6:] if m.role in {"user", "assistant"}
            ) or "(none)"
            prompt = SELECTION_PROMPT.format(
                max_skills=self.max_skills, catalog=self.skills.catalog_text(),
                history=history_text, hint=hint, request=request,
            )
            try:
                response = self.provider.generate([Message.user(prompt)], tools=None, temperature=0.0)
                data = extract_json(response.text)
            except ProviderError as exc:
                log.warning("planner call failed (%s); using keyword ranking", exc)
                data = None
            if data is not None and isinstance(data.get("skills"), list):
                names = []
                for name in data["skills"]:
                    name = str(name).strip()
                    if name in self.skills and name not in names:
                        names.append(name)
                    elif name:
                        log.info("planner proposed unknown skill '%s' (ignored)", name)
                plan = data.get("plan") or []
                if isinstance(plan, str):
                    plan = [plan]
                return SkillSelection(
                    skills=names[: self.max_skills],
                    reason=str(data.get("reason") or "")[:300],
                    plan=[str(step)[:200] for step in plan][:10],
                    source="llm",
                )

        # fallback: lexical ranking, keep only reasonably strong matches
        chosen = [meta.name for meta, score in ranked if score >= 1.5][: self.max_skills]
        return SkillSelection(
            skills=chosen,
            reason="selected by keyword matching" if chosen else "no matching skill",
            source="keywords" if chosen else "none",
        )
