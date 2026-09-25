"""Wire the components together (provider + skills + tools + memory -> Agent)."""
import logging
from typing import Any

from .config import Settings
from .core.agent import Agent
from .core.approval import ApprovalPolicy
from .memory.store import MemoryStore
from .providers import ModelProvider, create_provider
from .skills.manager import SkillManager
from .tools import ToolRegistry, default_registry


def build_agent(settings: Settings | None = None, *, provider: ModelProvider | None = None,
                approval_policy: ApprovalPolicy | None = None, tools: ToolRegistry | None = None,
                memory: MemoryStore | None = None, skills: SkillManager | None = None,
                execution_gateway: Any | None = None) -> Agent:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    provider = provider or create_provider(settings)
    skills = skills or SkillManager(settings.skills_dir)
    tools = tools or default_registry()
    memory = memory or MemoryStore(settings.db_path)
    return Agent(provider, skills, tools, memory, settings, approval_policy,
                 execution_gateway=execution_gateway)
