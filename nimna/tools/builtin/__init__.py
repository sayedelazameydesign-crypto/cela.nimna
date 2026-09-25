"""Built-in tool packs. Each module exposes ``register(registry)``."""
from ..base import ToolRegistry
from . import (
    browser_use,
    computer,
    csv_tools,
    edit,
    files,
    memory_tools,
    python_exec,
    reports,
    shell,
    skill_tools,
    vector_tools,
    web,
)

PACKS = {
    "edit": edit,  # T8 — precise file edit (Fabric-first)
    "browser_use": browser_use,
    "files": files,
    "csv": csv_tools,
    "python": python_exec,
    "shell": shell,  # registers only when SHELL_TOOL_ENABLED is on
    "web": web,
    "reports": reports,
    "memory": memory_tools,
    "vector": vector_tools,
    "skills": skill_tools,
    "computer": computer,
}


def register_all(registry: ToolRegistry, packs=None) -> ToolRegistry:
    for name, module in PACKS.items():
        if packs is None or name in packs:
            module.register(registry)
    return registry
