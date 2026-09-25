"""Swarm agents package."""
from .base import BaseSwarmAgent, SwarmResult
from .code_agent import CodeAgent
from .search_agent import SearchAgent
from .vision_agent import VisionAgent

AGENT_CLASSES = {
    "search": SearchAgent,
    "code": CodeAgent,
    "vision": VisionAgent,
}

__all__ = ["BaseSwarmAgent", "SwarmResult", "SearchAgent", "CodeAgent", "VisionAgent", "AGENT_CLASSES"]
