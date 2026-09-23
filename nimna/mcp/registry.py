"""Register governed MCP tools in the normal tool registry.

This is the bridge that makes remote MCP tools visible to the agent loop
without a second control path. Once registered, a remote tool is an ordinary
:class:`~nimna.tools.base.Tool`: the loop applies scope, policy and approval
exactly as it does for a local tool, and the handler forwards to
:meth:`MCPGateway.call_tool`.

Two design points carry the safety argument:

**Risk is always ``confirm``.** Forcing it is what makes the double gate sound.
The agent loop will not execute a ``confirm`` tool without approval, so by the
time the handler runs, the user has approved this call; the handler can pass
``explicit_consent=True`` to the gateway without weakening anything. If these
tools were registerable as ``safe``, the handler would run unapproved, and
``explicit_consent=True`` would be a bypass. It is therefore not configurable.

**The published schema is the server's.** ``inputSchema`` is forwarded to the
model as the tool's ``parameters``, so the model sees the parameter names the
server actually accepts. Local validation is a deliberate best-effort
pre-check, not a reimplementation of JSON Schema: the protocol revision widened
``inputSchema`` to any JSON Schema 2020-12, and the server remains the
authority. That asymmetry is intentional and stated rather than hidden.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict

from ..tools.base import Tool, ToolContext, ToolError, ToolRegistry
from .contract import ToolDefinition
from .gateway import MCPGateway
from .naming import (
    MCPNameError,
    encode_tool_name_element,
    namespaced_tool_name,
    validate_server_name,
)

log = logging.getLogger(__name__)

#: Tag applied to every registered remote tool.
MCP_TAG = "mcp"

#: Name of the gateway placed in ``ToolContext.extras``.
GATEWAY_KEY = "mcp_gateway"

#: Name of the approval ledger placed in ``ToolContext.extras``. The agent adds
#: a remote tool here only after the call cleared scope, policy and approval, so
#: the handler's ``explicit_consent=True`` is backed by evidence from the run
#: instead of being asserted by whoever built the context.
APPROVED_KEY = "mcp_approved_tools"

#: Stop a misbehaving server from flooding the registry (and the prompt).
DEFAULT_MAX_TOOLS_PER_SERVER = 64

#: Local name elements are derived, never taken from the server verbatim: the
#: registry, the capability scope and the model all see an escaped ASCII name,
#: while the wire keeps the server's own name. See :mod:`nimna.mcp.naming`.


class MCPToolError(ToolError):
    """A remote tool could not be called, reported back to the model."""


class MCPToolParams(BaseModel):
    """Permissive parameter model for a remote tool.

    Declares no fields on purpose. The authoritative parameter schema belongs
    to the MCP server and is published to the model verbatim via
    ``Tool.parameters``; mirroring it as pydantic fields here would be a second
    copy that can disagree with the first. Validation of individual fields
    happens in :func:`validate_arguments`, against the server's own schema, and
    the server remains the final authority.
    """

    model_config = ConfigDict(extra="allow")

    def arguments(self) -> dict[str, Any]:
        """The caller-supplied parameters, ready to forward."""
        return self.model_dump()


# --------------------------------------------------------------------------
# Best-effort local validation
# --------------------------------------------------------------------------

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _type_error(path: str, expected: str, value: Any) -> Optional[str]:
    types = _JSON_TYPES.get(expected)
    if types is None:
        # Unknown or composed type ("oneOf", "$ref" left unresolved, ...).
        # Not guessed at — the server validates it.
        return None
    if expected == "integer" and isinstance(value, bool):
        return f"{path} must be {expected}, got boolean"
    if expected == "number" and isinstance(value, bool):
        return f"{path} must be {expected}, got boolean"
    if not isinstance(value, types):
        return f"{path} must be {expected}, got {type(value).__name__}"
    return None


def validate_arguments(schema: Any, arguments: Mapping[str, Any]) -> None:
    """Pre-check ``arguments`` against an MCP ``inputSchema``.

    Checks required presence, unknown-parameter rejection (only when the schema
    closes the object), and primitive types for top-level ``properties``.

    Anything the schema expresses beyond that — composition, conditionals,
    ``$ref``, nested object shape, numeric bounds — is left to the server. This
    is intentionally partial: claiming full JSON Schema validation would be a
    claim this function cannot support.
    """
    if not isinstance(schema, Mapping):
        return
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}

    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        for name in required:
            if name not in arguments:
                raise MCPToolError(f"missing required parameter '{name}'")

    if schema.get("additionalProperties") is False and properties:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise MCPToolError(
                "unknown parameter(s) not accepted by this tool: " + ", ".join(unknown)
            )

    errors: list[str] = []
    for name, value in arguments.items():
        declared = properties.get(name)
        if not isinstance(declared, Mapping):
            continue
        expected = declared.get("type")
        if isinstance(expected, str):
            message = _type_error(name, expected, value)
            if message:
                errors.append(message)
    if errors:
        raise MCPToolError("invalid arguments for remote tool: " + "; ".join(errors))


# --------------------------------------------------------------------------
# Tool construction
# --------------------------------------------------------------------------


def local_tool_name(server: str, remote_name: str) -> str:
    """The registry-visible name for a remote tool.

    Exposed so a caller can compute a scope without building the tool first.
    """
    return namespaced_tool_name(server, remote_name)


def build_mcp_handler(
    gateway: MCPGateway, server: str, tool: str, schema: Any
) -> Callable[[BaseModel, ToolContext], Any]:
    """Build the handler that forwards one remote tool call."""

    def handler(params: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        active = ctx.extras.get(GATEWAY_KEY)
        if active is None:
            # The handler is only ever registered by register_mcp_tools, which
            # also arranges the context, so this means the run was built
            # without the gateway: refuse rather than call an ungoverned path.
            raise MCPToolError(
                "MCP gateway is not available in this run; remote tools cannot be called"
            )
        arguments = params.arguments() if hasattr(params, "arguments") else dict(params)
        # Argument validation runs first: it is local, side-effect free, and a
        # clearer error for the model than "not approved". Refusing either way
        # happens before any request is built.
        validate_arguments(schema, arguments)

        approved = ctx.extras.get(APPROVED_KEY)
        # The ledger holds local names, because that is the name the agent gates
        # on; the wire name is not checked here.
        local_name = namespaced_tool_name(server, tool)
        if not approved or local_name not in approved:
            # Evidence, not assumption. The agent adds the tool here only after
            # the call survived scope, policy and the approval gate, so a caller
            # that invokes this handler directly — bypassing the agent loop —
            # fails closed instead of running with consent it granted itself.
            raise MCPToolError(
                f"remote tool {local_name!r} was invoked without an approval "
                "recorded for this call; only the agent loop may call a governed "
                "MCP tool"
            )

        outcome = active.call_tool(
            server,
            tool,
            arguments,
            # Reaching this handler means the agent loop already applied scope,
            # policy and the approval gate for a `confirm` tool (see module
            # docstring). Passing consent here stops the gateway asking the same
            # question a second time and deadlocking the call.
            explicit_consent=True,
            session_id=ctx.session_id,
            run_id=ctx.run_id,
        )
        if outcome.ok:
            return {
                "server": server,
                "tool": tool,
                "status": outcome.status,
                "result": outcome.result,
                "duration_ms": outcome.duration_ms,
            }
        # Refusals reach the model as errors it can explain, and remain recorded
        # as audit events by the gateway.
        raise MCPToolError(
            outcome.reason or f"remote tool {tool!r} on {server!r} did not complete"
        )

    return handler


def build_tool(
    gateway: MCPGateway,
    server: str,
    definition: ToolDefinition,
    *,
    description_limit: int = 1024,
) -> Tool:
    """Turn one ``tools/list`` entry into a registry tool.

    The tool is always ``confirm`` — see the module docstring for why that is
    the load-bearing part of the double-gate design.
    """
    # The server name is configuration and stays a short ASCII identifier.
    # The remote tool name is not: MCP allows any string, so it is escaped into
    # a local-safe element instead of being rejected. Rejecting it would make
    # an Arabic-named tool unusable for no security benefit.
    try:
        validate_server_name(server)
        encode_tool_name_element(definition.name)
    except MCPNameError:
        raise

    description = (definition.description or "").strip()
    if len(description) > description_limit:
        description = description[:description_limit].rstrip() + "…"
    if not description:
        description = f"Remote MCP tool '{definition.name}' on server '{server}'."

    return Tool(
        name=namespaced_tool_name(server, definition.name),
        description=f"[MCP:{server}] {description}",
        params_model=MCPToolParams,
        handler=build_mcp_handler(gateway, server, definition.name, definition.input_schema),
        risk="confirm",
        tags=[MCP_TAG, f"mcp:{server}"],
        parameters=dict(definition.input_schema or {}),
    )


@dataclass
class RegistrationReport:
    """What happened while publishing remote tools."""

    registered: dict[str, list[str]] = field(default_factory=dict)
    rejected: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(names) for names in self.registered.values())


def register_mcp_tools(
    registry: ToolRegistry,
    gateway: MCPGateway,
    *,
    servers: Optional[Sequence[str]] = None,
    max_tools_per_server: int = DEFAULT_MAX_TOOLS_PER_SERVER,
    replace: bool = False,
) -> RegistrationReport:
    """Publish every enabled server's tools into ``registry``.

    A server that cannot be reached is reported in ``errors`` and skipped: one
    broken endpoint must not stop the agent from starting, and it must not
    silently appear as "no tools".
    """
    report = RegistrationReport()
    for server in servers if servers is not None else gateway.server_names:
        try:
            definitions = gateway.list_tools(server)
        except Exception as exc:
            report.errors[server] = f"{type(exc).__name__}: {exc}"
            log.warning("MCP server %s unavailable during registration: %s", server, exc)
            continue

        rejected = gateway.rejected_tools(server)
        if rejected:
            report.rejected[server] = dict(rejected)

        names: list[str] = []
        for definition in list(definitions)[:max_tools_per_server]:
            try:
                tool = build_tool(gateway, server, definition)
                registry.register(tool, replace=replace)
            except Exception as exc:
                report.rejected.setdefault(server, {})[definition.name] = str(exc)
                continue
            names.append(tool.name)

        dropped = len(definitions) - len(names)
        if dropped > 0:
            log.warning(
                "MCP server %s published %d tool(s); %d withheld",
                server, len(names), dropped,
            )
        report.registered[server] = names
    return report


def attach_gateway(
    gateway: Optional[MCPGateway],
    extras: Optional[dict[str, Any]] = None,
    *,
    approved: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Return ``extras`` with the gateway and the approval ledger attached.

    ``approved`` is a mutable set the agent adds to at the execution point. It
    is deliberately not derived from the gateway: it records what *the agent*
    decided, so the handler's consent claim can be checked against it.
    """
    merged = dict(extras or {})
    if gateway is not None:
        merged[GATEWAY_KEY] = gateway
        merged.setdefault(APPROVED_KEY, approved if approved is not None else set())
    return merged


__all__ = [
    "APPROVED_KEY",
    "DEFAULT_MAX_TOOLS_PER_SERVER",
    "GATEWAY_KEY",
    "MCP_TAG",
    "MCPToolError",
    "local_tool_name",
    "MCPToolParams",
    "RegistrationReport",
    "attach_gateway",
    "build_mcp_handler",
    "build_tool",
    "register_mcp_tools",
    "validate_arguments",
]
