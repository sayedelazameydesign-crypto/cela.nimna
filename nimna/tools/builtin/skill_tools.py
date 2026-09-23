"""Tools that let the model pull in more skill context on demand
(progressive disclosure): load another skill or read a reference file."""
from pydantic import BaseModel, Field

from ..base import ToolContext, ToolError, ToolRegistry


class NoParams(BaseModel):
    """Tool takes no arguments."""


class LoadSkillParams(BaseModel):
    name: str = Field(..., description="Skill name exactly as listed in the catalog.")


class ReadReferenceParams(BaseModel):
    skill: str = Field(..., description="Skill name that owns the reference.")
    path: str = Field(..., description="Reference path relative to the skill folder, e.g. 'references/guide.md'.")
    max_chars: int = Field(12000, ge=500, le=60000)


def register(registry: ToolRegistry) -> None:
    @registry.tool("list_skills", "List all installed skills with their descriptions and triggers.",
                   NoParams, tags=["skills"])
    def list_skills(params: NoParams, ctx: ToolContext):
        if ctx.skills is None:
            raise ToolError("skill manager unavailable")
        return {"skills": [
            {"name": m.name, "description": m.description, "triggers": m.triggers,
             "allowed_tools": m.allowed_tools}
            for m in ctx.skills.list()
        ]}

    @registry.tool("load_skill", "Load the full instructions of an installed skill and unlock its tools for this task.",
                   LoadSkillParams, tags=["skills"])
    def load_skill(params: LoadSkillParams, ctx: ToolContext):
        if ctx.skills is None:
            raise ToolError("skill manager unavailable")
        try:
            skill = ctx.skills.get(params.name)
        except KeyError as exc:
            raise ToolError(str(exc))
        if ctx.on_skill_loaded is not None:
            ctx.on_skill_loaded(skill.name)
        return {
            "skill": skill.name,
            "version": skill.meta.version,
            "allowed_tools": skill.meta.allowed_tools,
            "references": skill.meta.references,
            "instructions": skill.instructions,
        }

    @registry.tool("read_skill_reference", "Read a reference document that belongs to a skill (references/ folder).",
                   ReadReferenceParams, tags=["skills"])
    def read_skill_reference(params: ReadReferenceParams, ctx: ToolContext):
        if ctx.skills is None:
            raise ToolError("skill manager unavailable")
        try:
            text = ctx.skills.read_reference(params.skill, params.path, max_chars=params.max_chars)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            raise ToolError(str(exc))
        return {"skill": params.skill, "path": params.path, "content": text}
