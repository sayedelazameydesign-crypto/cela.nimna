"""Wire the components together (provider + skills + tools + memory -> Agent)."""
import logging
from typing import Optional

from .config import Settings
from .core.agent import Agent
from .core.approval import ApprovalPolicy
from .memory.store import MemoryStore
from .providers import ModelProvider, create_provider
from .skills.manager import SkillManager
from .tools import ToolRegistry, default_registry


def build_agent(settings: Optional[Settings] = None, *, provider: Optional[ModelProvider] = None,
                approval_policy: Optional[ApprovalPolicy] = None, tools: Optional[ToolRegistry] = None,
                memory: Optional[MemoryStore] = None, skills: Optional[SkillManager] = None) -> Agent:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    provider = provider or create_provider(settings)
    skills = skills or SkillManager(settings.skills_dir)
    tools = tools or default_registry()
    # Sprint 3: auto-load plugins if present (non-blocking, best-effort)
    try:
        from pathlib import Path
        from .plugins.manager import load_plugins
        plugins_dir = Path("plugins")
        if plugins_dir.is_dir():
            loaded = load_plugins(tools, plugins_dir, skills_manager=skills)
            if loaded:
                logging.getLogger(__name__).info("plugins loaded: %s", loaded)
    except Exception as exc:
        logging.getLogger(__name__).warning("plugin load failed: %s", exc)
    memory = memory or MemoryStore(settings.db_path)
    return Agent(provider, skills, tools, memory, settings, approval_policy)
