"""Parse ``SKILL.md`` files (YAML front matter + markdown body)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .models import Skill, SkillMeta

FRONT_MATTER_RE = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

# keys accepted in the front matter and their canonical names
KEY_ALIASES = {
    "allowed-tools": "allowed_tools",
    "allowedtools": "allowed_tools",
    "tools": "allowed_tools",
    "display-name": "display_name",
    "title": "display_name",
    "keywords": "triggers",
    "risk": "risk_level",
    "risk-level": "risk_level",
    "risk_level": "risk_level",
}


class SkillParseError(ValueError):
    pass


def _lenient_front_matter(raw: str) -> dict[str, Any]:
    """Fallback for front matter that is not strict YAML.

    Skill authors (humans and LLMs alike) often write ``description: A: B`` or
    forget to quote colons.  This parser understands the small subset we need:
    ``key: value``, ``key: [a, b]`` and ``key:`` followed by ``- item`` lines.
    """
    data: dict[str, Any] = {}
    current: str | None = None
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current is not None:
            data.setdefault(current, [])
            if isinstance(data[current], list):
                data[current].append(stripped[2:].strip().strip("\"'"))
            continue
        if ":" not in stripped or line.startswith((" ", "\t")):
            continue
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        current = key
        if not value:
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        else:
            data[key] = value.strip("\"'")
    return data


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    raw_yaml, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        data = _lenient_front_matter(raw_yaml)
        if not data:
            raise SkillParseError("invalid YAML front matter (could not parse even leniently)")
    if not isinstance(data, dict):
        raise SkillParseError("front matter must be a mapping")
    return data, body


def _normalise_keys(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        canonical = KEY_ALIASES.get(str(key).lower(), str(key).lower().replace("-", "_"))
        out[canonical] = value
    return out


def _discover_references(directory: Path) -> list[str]:
    refs_dir = directory / "references"
    if not refs_dir.is_dir():
        return []
    files = sorted(p for p in refs_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".csv"})
    return [str(p.relative_to(directory)).replace("\\", "/") for p in files][:50]


def parse_skill_text(text: str, *, path: Path, directory: Path) -> Skill:
    data, body = split_front_matter(text)
    data = _normalise_keys(data)
    data.setdefault("name", directory.name)
    if "description" not in data:
        # fall back to the first non-heading paragraph of the body
        for para in re.split(r"\n\s*\n", body.strip()):
            para = para.strip()
            if para and not para.startswith("#"):
                data["description"] = para
                break
    known = {"name", "description", "display_name", "version", "triggers", "allowed_tools",
             "tags", "risk_level", "metadata"}
    metadata = dict(data.get("metadata") or {})
    for key in list(data.keys()):
        if key not in known:
            metadata[key] = data.pop(key)
    data["metadata"] = metadata
    if "version" in data:
        data["version"] = str(data["version"])
    data["references"] = _discover_references(directory)
    try:
        meta = SkillMeta(**data)
    except Exception as exc:
        raise SkillParseError(f"{path}: {exc}") from exc
    return Skill(meta=meta, instructions=body.strip(), path=str(path), directory=str(directory))


def parse_skill_file(path: Path) -> Skill:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillParseError(f"cannot read {path}: {exc}") from exc
    return parse_skill_text(text, path=path, directory=path.parent)
