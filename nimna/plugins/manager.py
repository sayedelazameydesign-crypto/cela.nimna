"""Dynamic Plugin Loader (Sprint 3 scaffold).

Scans `plugins/` + `nimna/plugins/` external dirs, validates manifest.json,
and registers tools into ToolRegistry at startup via `register_all`.

Usage:
  from nimna.plugins.manager import load_plugins
  load_plugins(registry, Path("plugins"))
"""
from __future__ import annotations
import importlib.util, sys
from pathlib import Path
from typing import Any
from .schema import PluginManifest

def discover_plugins(plugins_dir: Path = Path("plugins")) -> list[tuple[Path, PluginManifest]]:
    out=[]
    if not plugins_dir.is_dir():
        return out
    for child in plugins_dir.iterdir():
        if not child.is_dir(): continue
        mf = child / "manifest.json"
        if not mf.is_file(): continue
        try:
            man = PluginManifest.load(mf)
            out.append((child, man))
        except Exception as e:
            print(f"[plugins] invalid {mf}: {e}")
    return out

def load_plugins(registry: Any, plugins_dir: Path = Path("plugins"), skills_manager: Any = None) -> list[str]:
    loaded=[]
    for plugin_path, manifest in discover_plugins(plugins_dir):
        # register tools via import
        for tool_spec in manifest.tools:
            try:
                mod_path, func = tool_spec.entrypoint.split(":")
                file = plugin_path / mod_path
                spec = importlib.util.spec_from_file_location(f"plugin_{manifest.name}_{tool_spec.name}", file)
                mod = importlib.util.module_from_spec(spec)  # type: ignore
                sys.modules[spec.name] = mod  # type: ignore
                spec.loader.exec_module(mod)  # type: ignore
                getattr(mod, func)(registry, tool_spec)  # plugin's register(registry, spec)
                loaded.append(f"{manifest.name}:{tool_spec.name}")
            except Exception as e:
                print(f"[plugins] failed {manifest.name}:{tool_spec.name}: {e}")
        # expose SKILL.md folders to skills manager if present
        if skills_manager is not None:
            for skill in manifest.skills:
                # symlink or copy? For scaffold just log
                pass
    return loaded

