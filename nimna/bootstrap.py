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


def build_gateway(settings: Settings, *, evidence=None):  # -> Optional[MCPGateway]
    """Build the MCP gateway when it is enabled, otherwise return ``None``.

    Returns ``None`` rather than a disabled gateway so that a misconfigured
    environment cannot leave a half-live gateway attached to the agent. Any
    configuration error is raised: a server entry that cannot be built is a
    mistake to fix, not something to start without.
    """
    if not getattr(settings, "mcp_enabled", False):
        return None
    from .mcp import MCPGateway

    return MCPGateway.from_settings(
        settings,
        evidence=evidence,
        allow_private_networks=bool(getattr(settings, "mcp_allow_private_networks", False)),
    )


def build_agent(settings: Optional[Settings] = None, *, provider: Optional[ModelProvider] = None,
                approval_policy: Optional[ApprovalPolicy] = None, tools: Optional[ToolRegistry] = None,
                memory: Optional[MemoryStore] = None, skills: Optional[SkillManager] = None,
                mcp_gateway: Optional[object] = None) -> Agent:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    provider = provider or create_provider(settings)
    skills = skills or SkillManager(settings.skills_dir)
    tools = tools or default_registry()
    memory = memory or MemoryStore(settings.db_path)

    from .evidence import EvidenceJournal

    evidence = EvidenceJournal(memory)
    if mcp_gateway is None:
        mcp_gateway = build_gateway(settings, evidence=evidence)
    if mcp_gateway is not None:
        # Publishing is best-effort by design: one unreachable server must not
        # stop the agent from starting, and the report says what was withheld.
        from .mcp.registry import register_mcp_tools

        report = register_mcp_tools(tools, mcp_gateway)  # type: ignore[arg-type]
        for server, names in report.registered.items():
            logging.getLogger(__name__).info("MCP server %s published %d tool(s)", server, len(names))
        for server, error in report.errors.items():
            logging.getLogger(__name__).warning("MCP server %s not published: %s", server, error)

    return Agent(provider, skills, tools, memory, settings, approval_policy,
                 mcp_gateway=mcp_gateway)
