from .base import (
    Risk,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolValidationError,
    serialize_result,
)


def default_registry() -> ToolRegistry:
    """Registry with every built-in tool pack installed."""
    from .builtin import register_all

    return register_all(ToolRegistry())


__all__ = [
    "Risk",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolValidationError",
    "serialize_result",
    "default_registry",
]
