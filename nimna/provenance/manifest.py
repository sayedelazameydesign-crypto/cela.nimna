"""Runtime and repository fingerprints without leaking secrets.

A manifest identifies the runtime that produced evidence: source files, skills,
tools, provider/model and bounded configuration.  It is a fingerprint, not a
cryptographic signature; signing can be layered on in deployment.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .hashchain import sha256_hex

_MAX_FILE_BYTES = 2_000_000
_EXCLUDED_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "node_modules", "data"}
_EXCLUDED_NAMES = {".env", ".env.local", ".env.production"}


def _git_commit(root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        value = result.stdout.strip()
        return value or None
    except Exception:
        return None


def _file_digest(path: Path) -> Optional[str]:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def repository_fingerprint(root: Path | str = ".") -> dict[str, Any]:
    root = Path(root).resolve()
    files: list[dict[str, str]] = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            relative_parts = set(path.relative_to(root).parts)
            if relative_parts & _EXCLUDED_DIRS or path.name in _EXCLUDED_NAMES or any(part.endswith(".egg-info") for part in relative_parts):
                continue
            digest = _file_digest(path)
            if digest:
                files.append({"path": str(path.relative_to(root)), "sha256": digest})
    payload = {"root": str(root), "files": files}
    return {"files": files, "file_count": len(files), "sha256": sha256_hex(payload)}


def _safe_settings(settings: Any) -> dict[str, Any]:
    keys = (
        "provider", "model_name", "sandbox_backend", "max_steps", "max_tool_calls",
        "max_runtime_seconds", "cost_guard_enabled", "cost_guard_hard", "max_spend_usd",
        "browser_use_enabled", "browser_use_base_url", "browser_use_reasoning_effort", "browser_use_max_spend_usd",
    )
    result: dict[str, Any] = {}
    for key in keys:
        try:
            value = getattr(settings, key)
            if callable(value):
                value = value()
            # Never include values that look like credentials.
            if "key" in key.lower() or "token" in key.lower() or "secret" in key.lower():
                continue
            result[key] = value
        except Exception:
            continue
    return result


def build_manifest(
    *,
    settings: Any = None,
    provider: Optional[Mapping[str, Any]] = None,
    skills: Optional[Iterable[Any]] = None,
    tools: Optional[Iterable[Any]] = None,
    root: Path | str = ".",
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    skill_ids: list[str] = []
    for skill in skills or ():
        try:
            meta = skill.meta
            skill_ids.append(f"{meta.name}@{meta.version}")
        except Exception:
            skill_ids.append(str(skill))
    tool_ids: list[str] = []
    for tool in tools or ():
        tool_ids.append(str(getattr(tool, "name", tool)))
    provider_data = dict(provider or {})
    # Strip potentially sensitive provider fields if a custom provider returns any.
    provider_data = {
        key: value
        for key, value in provider_data.items()
        if "key" not in key.lower() and "token" not in key.lower() and "secret" not in key.lower()
    }
    manifest: dict[str, Any] = {
        "schema": "nimna.runtime-manifest.v1",
        "git_commit": _git_commit(root_path),
        "python": platform.python_version(),
        "platform": sys.platform,
        "provider": provider_data,
        "settings": _safe_settings(settings) if settings is not None else {},
        "skills": sorted(skill_ids),
        "tools": sorted(tool_ids),
        "repository": repository_fingerprint(root_path),
    }
    manifest["sha256"] = sha256_hex({key: value for key, value in manifest.items() if key != "sha256"})
    return manifest


__all__ = ["build_manifest", "repository_fingerprint"]
