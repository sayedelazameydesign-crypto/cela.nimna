"""Plugin SDK — manifest.json schema (Sprint 3).

Plugin = folder `plugins/<name>/` with:
  manifest.json  {name, version, display_name, description, author, tools:[{name, entrypoint, risk}]}
  SKILL.md       (optional, for planner)
  tools/*.py     (optional, auto-loaded)

Example plugins/example-hello/manifest.json
"""
from __future__ import annotations
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

class PluginToolSpec(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_]{3,32}$")
    entrypoint: str = Field(..., description="python file e.g. tools/hello.py:register")
    description: str = Field(..., max_length=200)
    risk: Literal["safe","confirm","restricted"] = "safe"
    tags: list[str] = Field(default_factory=list)

class PluginManifest(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_-]{3,32}$")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    display_name: str = Field(..., max_length=60)
    description: str = Field(..., max_length=300)
    author: str = Field(default="community")
    homepage: str | None = None
    tools: list[PluginToolSpec] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, description="SKILL.md subfolders exposed")
    min_nimna_version: str = Field(default="0.2.0")

    @classmethod
    def load(cls, path: Path) -> "PluginManifest":
        import json
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

