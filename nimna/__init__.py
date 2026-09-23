"""Nimna – a reusable-skills agent.

Quick start::

    from nimna import build_agent
    agent = build_agent()                 # reads .env / environment
    result = agent.run("حلّل ملف sales.csv وأنشئ ملخصًا")
    print(result.reply)
"""
from .bootstrap import build_agent
from .config import Settings

__version__ = "0.1.0"
__all__ = ["build_agent", "Settings", "__version__"]
