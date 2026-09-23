"""Skill data model.

A skill is a directory containing ``SKILL.md`` (YAML front matter + markdown
instructions) plus optional ``references/``, ``scripts/`` and ``tests/``.
Only the front matter is shown to the planner; the body is loaded on demand
(progressive disclosure).
"""
import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEMVER_RE = re.compile(r"^\d+(\.\d+){0,2}([-.+].*)?$")


class SkillMeta(BaseModel):
    name: str
    description: str
    display_name: Optional[str] = None
    version: str = "0.1.0"
    triggers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    risk_level: Literal["safe", "confirm", "restricted"] = "safe"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        value = value.strip()
        if not NAME_RE.match(value):
            raise ValueError(
                f"invalid skill name '{value}': use lowercase letters, digits, '-' or '_'"
            )
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("description is required")
        return value[:1200]

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        value = str(value).strip()
        if not SEMVER_RE.match(value):
            raise ValueError(f"version '{value}' should be semver-like (e.g. 1.0.0)")
        return value

    @field_validator("risk_level", mode="before")
    @classmethod
    def _coerce_risk(cls, value: Any) -> str:
        if value is None or value == "":
            return "safe"
        value = str(value).strip().lower()
        if value not in {"safe", "confirm", "restricted"}:
            raise ValueError(f"risk_level must be safe | confirm | restricted, got '{value}'")
        return value

    @field_validator("triggers", "allowed_tools", "tags", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            parts = re.split(r"[,\n]", value) if "," in value or "\n" in value else value.split()
            return [p.strip() for p in parts if p.strip()]
        return [str(v).strip() for v in value if str(v).strip()]

    @property
    def title(self) -> str:
        return self.display_name or self.name

    def catalog_line(self) -> str:
        triggers = f" | triggers: {', '.join(self.triggers[:8])}" if self.triggers else ""
        tools = f" | tools: {', '.join(self.allowed_tools)}" if self.allowed_tools else ""
        risk = f" | risk: {self.risk_level}" if self.risk_level != "safe" else ""
        return f"- {self.name}: {self.description}{triggers}{tools}{risk}"

    def unknown_tools(self, registry_names: set[str]) -> list[str]:
        return [t for t in self.allowed_tools if t not in registry_names]


class Skill(BaseModel):
    meta: SkillMeta
    instructions: str
    path: str
    directory: str

    @property
    def name(self) -> str:
        return self.meta.name

    def as_prompt_block(self) -> str:
        header = f"### Skill: {self.meta.name}"
        if self.meta.display_name:
            header += f" ({self.meta.display_name})"
        refs = ""
        if self.meta.references:
            refs = (
                "\n\nReference files (load with read_skill_reference when needed): "
                + ", ".join(self.meta.references)
            )
        return f"{header}\nversion: {self.meta.version}  risk: {self.meta.risk_level}\n\n{self.instructions.strip()}{refs}"

    def resolve_reference(self, relative: str) -> Path:
        base = Path(self.directory).resolve()
        target = (base / relative).resolve()
        if base not in target.parents and target != base:
            raise ValueError("reference path escapes the skill directory")
        return target
