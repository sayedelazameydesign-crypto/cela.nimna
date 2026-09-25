"""SkillManager: discovers skills, serves the compact catalog and full bodies."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .loader import SkillParseError, parse_skill_file
from .models import Skill, SkillMeta

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)
_AR_PREFIXES = ("وال", "بال", "كال", "فال", "ال", "لل", "و", "ب", "ل", "ف", "ك")


def _tokens(text: str) -> list[str]:
    return [_normalise(tok) for tok in _WORD_RE.findall(text.lower()) if len(tok) > 1]


def _normalise(token: str) -> str:
    """Light Arabic/English normalisation for keyword matching."""
    token = re.sub("[\u064B-\u0652]", "", token)  # strip tashkeel
    token = token.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    for prefix in _AR_PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= 3:
            token = token[len(prefix):]
            break
    if token.endswith("s") and len(token) > 4:  # crude english plural
        token = token[:-1]
    return token


class SkillManager:
    def __init__(self, skills_dir: Path | str):
        self.skills_dir = Path(skills_dir)
        self._skills: dict[str, Skill] = {}
        self.errors: dict[str, str] = {}
        self.reload()

    # -- discovery -------------------------------------------------------
    def reload(self) -> None:
        self._skills.clear()
        self.errors.clear()
        if not self.skills_dir.is_dir():
            log.warning("skills directory %s does not exist", self.skills_dir)
            return
        for skill_file in sorted(self.skills_dir.glob("*/SKILL.md")):
            try:
                skill = parse_skill_file(skill_file)
            except SkillParseError as exc:
                self.errors[skill_file.parent.name] = str(exc)
                log.warning("skipping skill %s: %s", skill_file.parent.name, exc)
                continue
            if skill.name in self._skills:
                self.errors[skill.name] = f"duplicate skill name in {skill_file}"
                continue
            self._skills[skill.name] = skill
        log.info("loaded %d skills from %s", len(self._skills), self.skills_dir)

    # -- access ----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        return list(self._skills)

    def list(self) -> list[SkillMeta]:
        return [skill.meta for skill in self._skills.values()]

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"unknown skill '{name}'. Available: {', '.join(self.names()) or 'none'}")

    def find(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def catalog_text(self) -> str:
        """Compact catalog (front matter only) used by the planner."""
        if not self._skills:
            return "(no skills installed)"
        return "\n".join(skill.meta.catalog_line() for skill in self._skills.values())

    # -- retrieval -------------------------------------------------------
    def rank_by_keywords(self, query: str, limit: int = 5) -> list[tuple[SkillMeta, float]]:
        """Cheap lexical ranking used as a pre-filter / fallback for the LLM planner."""
        query_tokens = set(_tokens(query))
        query_lower = query.lower()
        scored: list[tuple[SkillMeta, float]] = []
        for skill in self._skills.values():
            meta = skill.meta
            score = 0.0
            for trigger in meta.triggers:
                if trigger.lower() in query_lower:
                    score += 3.0
                else:
                    overlap = query_tokens & set(_tokens(trigger))
                    score += 1.5 * len(overlap)
            if meta.name.replace("_", " ") in query_lower or meta.name in query_lower:
                score += 3.0
            desc_tokens = set(_tokens(meta.description)) | set(_tokens(" ".join(meta.tags)))
            score += 0.5 * len(query_tokens & desc_tokens)
            if score > 0:
                scored.append((meta, round(score, 2)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    # -- references ------------------------------------------------------
    def read_reference(self, skill_name: str, relative: str, max_chars: int = 20000) -> str:
        skill = self.get(skill_name)
        target = skill.resolve_reference(relative)
        if not target.is_file():
            raise FileNotFoundError(
                f"reference '{relative}' not found in skill '{skill_name}'. "
                f"Available: {', '.join(skill.meta.references) or 'none'}"
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... truncated, {len(text) - max_chars} more characters]"
        return text

    def validate_all(self, registry_names: set[str] | None = None) -> dict[str, list[str]]:
        """Return {skill_name: [warnings]} for every discovered skill.

        When ``registry_names`` is supplied, unknown ``allowed_tools`` are flagged.
        """
        report: dict[str, list[str]] = {}
        for skill in self._skills.values():
            warnings: list[str] = []
            if not skill.meta.triggers:
                warnings.append("no triggers defined (planner relies on description only)")
            if not skill.instructions:
                warnings.append("empty instructions body")
            if len(skill.instructions) > 15000:
                warnings.append("instructions exceed 15k characters; move detail into references/")
            if registry_names is not None:
                unknown = skill.meta.unknown_tools(registry_names)
                if unknown:
                    warnings.append(f"references unknown tools: {', '.join(unknown)} (will be ignored at runtime)")
            if skill.meta.risk_level == "restricted":
                warnings.append("skill marked restricted – requires manual review before use")
            report[skill.name] = warnings
        for name, error in self.errors.items():
            report[name] = [f"ERROR: {error}"]
        return report
