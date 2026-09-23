"""Governed gateway to external MCP servers.

An MCP server is a third party that advertises tools and executes them with
whatever authority it holds. The gateway's job is to make that authority
Nimna's decision rather than the server's:

* **Scope.** A server's tools are published under a namespaced name and only
  reachable when they are inside the active capability scope. A tool outside
  the scope is denied before a request is built.
* **Policy.** Declared risk flows through the same :class:`PolicyEngine` the
  local tools use, so a remote tool cannot take a path around the approval
  gate that a local tool has to stop at.
* **Reach.** Every endpoint is checked against the SSRF guard before the first
  request, so an MCP server URL cannot be used to reach the host's own network.
* **Failure.** Every failure mode — unknown server, disabled server, missing
  credential, private address, policy denial, transport breakage — is a refusal.
  Nothing is retried silently and nothing is allowed to degrade into an
  unauthenticated or unscoped call.

Every decision, including refusals, is recorded as an audit event. Expected
failures are returned as outcomes rather than raised, matching
``ToolRegistry.execute`` — the caller decides what to tell the model.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from ..governance.policy import PolicyDecision, PolicyEngine, ToolRisk
from .auth import MCPAuthError, MCPCredential, describe_credential, redact_headers
from .contract import (
    MCPProtocolError,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    PROTOCOL_VERSION,
    DiscoveryResult,
    MCPResponse,
    ToolDefinition,
    build_discover_request,
    build_request,
    parse_discover_result,
    parse_tool_list,
    validate_cacheable_result,
)
from .headers import filter_tool_definitions
from .transport import (
    MCPLegacyServerError,
    MCPTransport,
    MCPTransportError,
    StreamableHTTPTransport,
)

#: Separator for namespaced tool names. Double underscore so that a server name
#: cannot collide with a hyphenated local tool name.
NAMESPACE_SEPARATOR = "__"
NAMESPACE_PREFIX = "mcp"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")

#: Only these transports may be registered. `stdio` would mean launching a
#: subprocess, and this repository is explicit that a subprocess is not a
#: security boundary — so a subprocess transport is out of scope here rather
#: than quietly inherited.
ALLOWED_TRANSPORTS: frozenset[str] = frozenset({"streamable_http"})


class MCPGatewayError(RuntimeError):
    """The gateway cannot be configured as asked."""


def namespaced_tool_name(server: str, tool: str) -> str:
    return f"{NAMESPACE_PREFIX}{NAMESPACE_SEPARATOR}{server}{NAMESPACE_SEPARATOR}{tool}"


def split_namespaced_tool(name: str) -> Optional[tuple[str, str]]:
    """Inverse of :func:`namespaced_tool_name`, or ``None`` if not namespaced."""
    prefix = f"{NAMESPACE_PREFIX}{NAMESPACE_SEPARATOR}"
    if not str(name).startswith(prefix):
        return None
    remainder = str(name)[len(prefix) :]
    server, separator, tool = remainder.partition(NAMESPACE_SEPARATOR)
    if not separator or not server or not tool:
        return None
    return server, tool


@dataclass(frozen=True)
class MCPServerConfig:
    """One governed MCP server entry."""

    name: str
    url: str
    transport: str = "streamable_http"
    enabled: bool = True
    #: Environment variable holding the credential. Required unless
    #: ``require_auth`` is explicitly turned off.
    credential_env: Optional[str] = None
    credential_header: str = "Authorization"
    credential_scheme: str = "Bearer"
    require_auth: bool = True
    #: Declared risk used by the policy engine for this server's tools.
    declare_risk: str = "confirm"
    #: Capability scope. ``None`` means "everything this server advertises",
    #: which is still subject to policy and to ``enabled``.
    allowed_tools: Optional[frozenset[str]] = None
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if not _NAME_RE.match(str(self.name or "")):
            raise MCPGatewayError(
                "server name must be a lowercase identifier matching "
                f"{_NAME_RE.pattern!r}, got {self.name!r}"
            )
        if not str(self.url or "").strip():
            raise MCPGatewayError(f"server {self.name!r} requires a URL")
        if self.transport not in ALLOWED_TRANSPORTS:
            raise MCPGatewayError(
                f"server {self.name!r} requests transport {self.transport!r}; "
                f"supported: {sorted(ALLOWED_TRANSPORTS)}"
            )
        if not self.require_auth and self.credential_env:
            # Not contradictory, but surprising enough to be worth naming: a
            # credential that is configured but not required will not be sent.
            pass
        if any(ch in str(self.url) for ch in "\r\n"):
            raise MCPGatewayError(f"server {self.name!r} URL must not contain CR or LF")

    def permits(self, tool_name: str) -> bool:
        if self.allowed_tools is None:
            return True
        return tool_name in self.allowed_tools


@dataclass(frozen=True)
class ToolCallOutcome:
    """The result of a governed tool call.

    ``status`` is one of ``ok``, ``denied``, ``approval_required``, ``error``.
    Refusals carry a ``reason`` that is safe to show a user; ``detail`` carries
    the underlying message for the operator, already redacted.
    """

    status: str
    server: str
    tool: str
    result: Any = None
    reason: str = ""
    detail: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _env_flag(value: Optional[str], default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def servers_from_env(env: Optional[Mapping[str, str]] = None) -> tuple[MCPServerConfig, ...]:
    """Build server entries from environment variables.

    ``MCP_SERVERS`` lists the names; each name is described by
    ``MCPSERVER_<NAME>_*`` variables::

        MCP_SERVERS=demo
        MCPSERVER_DEMO_URL=https://mcp.example.com/mcp
        MCPSERVER_DEMO_TOKEN_ENV=DEMO_MCP_TOKEN   # name of the var holding the secret
        MCPSERVER_DEMO_SCOPE=get_weather,list_files
        MCPSERVER_DEMO_RISK=confirm
        MCPSERVER_DEMO_AUTH=true
        MCPSERVER_DEMO_ENABLED=true
        MCPSERVER_DEMO_TIMEOUT=60

    The secret itself is never read here. ``TOKEN_ENV`` carries the *name* of
    the variable that holds it, so a config object can be logged, compared or
    dumped without ever containing credential material.
    """
    env = os.environ if env is None else env
    raw_names = str(env.get("MCP_SERVERS") or "")
    names = [part.strip() for part in raw_names.split(",") if part.strip()]

    servers: list[MCPServerConfig] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise MCPGatewayError(f"duplicate server name {name!r} in MCP_SERVERS")
        seen.add(name)
        prefix = f"MCPSERVER_{name.upper().replace('-', '_')}_"

        url = str(env.get(f"{prefix}URL") or "").strip()
        if not url:
            raise MCPGatewayError(
                f"MCP_SERVERS lists {name!r} but {prefix}URL is not set"
            )
        scope_raw = str(env.get(f"{prefix}SCOPE") or "").strip()
        scope = (
            frozenset(part.strip() for part in scope_raw.split(",") if part.strip())
            if scope_raw
            else None
        )
        timeout_raw = str(env.get(f"{prefix}TIMEOUT") or "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else 60.0
        except ValueError as exc:
            raise MCPGatewayError(f"{prefix}TIMEOUT must be a number") from exc

        servers.append(
            MCPServerConfig(
                name=name,
                url=url,
                transport=str(env.get(f"{prefix}TRANSPORT") or "streamable_http").strip(),
                enabled=_env_flag(env.get(f"{prefix}ENABLED"), True),
                credential_env=str(env.get(f"{prefix}TOKEN_ENV") or f"{prefix}TOKEN").strip(),
                credential_header=str(env.get(f"{prefix}TOKEN_HEADER") or "Authorization").strip(),
                credential_scheme=str(env.get(f"{prefix}TOKEN_SCHEME") or "Bearer").strip(),
                require_auth=_env_flag(env.get(f"{prefix}AUTH"), True),
                declare_risk=str(env.get(f"{prefix}RISK") or "confirm").strip().lower(),
                allowed_tools=scope,
                timeout=timeout,
            )
        )
    return tuple(servers)


@dataclass
class _ServerState:
    config: MCPServerConfig
    transport: MCPTransport
    credential: Optional[MCPCredential] = None
    tools: dict[str, ToolDefinition] = field(default_factory=dict)
    definitions: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    rejected_tools: dict[str, str] = field(default_factory=dict)


class MCPGateway:
    """Owns the MCP server registry, policy gate and evidence trail."""

    def __init__(
        self,
        servers: Sequence[MCPServerConfig] = (),
        *,
        enabled: bool = False,
        policy: Optional[PolicyEngine] = None,
        evidence: Any = None,
        allow_private_networks: bool = False,
        client_name: str = "nimna",
        client_version: str = "0.1.0",
        protocol_version: str = PROTOCOL_VERSION,
        max_response_bytes: int = 8 * 1024 * 1024,
        transport_factory: Optional[Callable[[MCPServerConfig, Optional[MCPCredential]], MCPTransport]] = None,
        credential_loader: Optional[Callable[[MCPServerConfig], Optional[MCPCredential]]] = None,
    ):
        self.enabled = bool(enabled)
        self.policy = policy or PolicyEngine()
        self.evidence = evidence
        self.allow_private_networks = bool(allow_private_networks)
        self.client_name = client_name
        self.client_version = client_version
        self.protocol_version = protocol_version
        self.max_response_bytes = max_response_bytes
        self._transport_factory = transport_factory or self._default_transport_factory
        self._credential_loader = credential_loader or self._default_credential_loader
        self._states: dict[str, _ServerState] = {}
        self._request_counter = 0

        for config in servers:
            if config.name in self._states:
                raise MCPGatewayError(f"duplicate MCP server name {config.name!r}")
            self._states[config.name] = self._build_state(config)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        servers: Optional[Sequence[MCPServerConfig]] = None,
        **kwargs: Any,
    ) -> "MCPGateway":
        """Build a gateway whose enablement follows ``Settings.mcp_enabled``.

        ``servers`` defaults to whatever ``MCP_SERVERS`` describes, so the
        settings object plus the environment is enough to configure a gateway.
        """
        if servers is None:
            servers = servers_from_env()
        return cls(
            servers,
            enabled=bool(getattr(settings, "mcp_enabled", False)),
            max_response_bytes=int(
                getattr(settings, "mcp_max_response_bytes", 8 * 1024 * 1024)
            ),
            **kwargs,
        )

    def _default_credential_loader(self, config: MCPServerConfig) -> Optional[MCPCredential]:
        from .auth import credential_from_env

        return credential_from_env(
            env_var=config.credential_env,
            header=config.credential_header,
            scheme=config.credential_scheme,
        )

    def _default_transport_factory(
        self, config: MCPServerConfig, credential: Optional[MCPCredential]
    ) -> MCPTransport:
        return StreamableHTTPTransport(
            config.url,
            credential=credential,
            protocol_version=self.protocol_version,
            timeout=config.timeout,
            max_response_bytes=self.max_response_bytes,
        )

    def _build_state(self, config: MCPServerConfig) -> _ServerState:
        credential: Optional[MCPCredential] = None
        if config.require_auth:
            credential = self._credential_loader(config)
            if credential is None:
                # Fail closed at construction: a server that requires auth and
                # has none is a misconfiguration, and discovering that on first
                # tool call would be discovering it during a run.
                raise MCPAuthError(
                    f"MCP server {config.name!r} requires authentication but "
                    f"credential env var {config.credential_env!r} is not set"
                )
        else:
            credential = self._credential_loader(config)
        # Validate reachability policy once, before any request exists.
        self._assert_reachable(config)
        transport = self._transport_factory(config, credential)
        return _ServerState(config=config, transport=transport, credential=credential)

    def _assert_reachable(self, config: MCPServerConfig) -> None:
        if self.allow_private_networks:
            return
        # Reuse the same SSRF guard the web tools use rather than growing a
        # second, weaker one: a private address is equally reachable whichever
        # tool asked for it.
        from ..tools.builtin.web import assert_public_url

        assert_public_url(config.url)

    # -- introspection ---------------------------------------------------

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(self._states)

    def config(self, server: str) -> MCPServerConfig:
        state = self._require_server(server)
        return state.config

    def capability_scope(self, server: str) -> frozenset[str]:
        """Namespaced tool names this server may expose, per its scope."""
        state = self._require_server(server)
        if state.config.allowed_tools is not None:
            return frozenset(
                namespaced_tool_name(server, tool) for tool in state.config.allowed_tools
            )
        return frozenset(namespaced_tool_name(server, tool) for tool in state.tools)

    def rejected_tools(self, server: str) -> Mapping[str, str]:
        """Tool definitions excluded because their headers violate the contract."""
        return dict(self._require_server(server).rejected_tools)

    # -- requests --------------------------------------------------------

    def _next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    def _require_server(self, server: str) -> _ServerState:
        state = self._states.get(server)
        if state is None:
            raise MCPGatewayError(f"unknown MCP server {server!r}")
        return state

    def _record(self, session_id: Optional[str], run_id: Optional[str], event: str, payload: Mapping[str, Any]) -> None:
        if self.evidence is None:
            return
        try:
            self.evidence.record(session_id, run_id, event, dict(payload))
        except Exception:  # pragma: no cover - audit must not break a call
            # An audit sink that raises must not turn a successful call into a
            # failure, but it must also not be silent.
            import logging

            logging.getLogger(__name__).exception("failed to record MCP evidence event %s", event)

    def discover(
        self, server: str, *, session_id: Optional[str] = None, run_id: Optional[str] = None
    ) -> DiscoveryResult:
        """Call ``server/discover`` and return what the server advertises."""
        self._assert_enabled(server)
        state = self._require_server(server)
        request = build_discover_request(
            self._next_id(), client_name=self.client_name, client_version=self.client_version
        )
        response = self._send(state, request)
        result = response.unwrap()
        discovery = parse_discover_result(result.value)
        self._record(
            session_id,
            run_id,
            "mcp.discover",
            {
                "server": server,
                "protocol_versions": list(discovery.protocol_versions),
                "supports_current_revision": discovery.supports_current_revision,
                "server_info": dict(discovery.server_info),
                "credential": describe_credential(state.credential),
            },
        )
        return discovery

    def list_tools(
        self, server: str, *, session_id: Optional[str] = None, run_id: Optional[str] = None
    ) -> tuple[ToolDefinition, ...]:
        """Fetch ``tools/list``, excluding definitions with invalid headers."""
        self._assert_enabled(server)
        state = self._require_server(server)
        request = build_request(
            self._next_id(),
            METHOD_TOOLS_LIST,
            client_name=self.client_name,
            client_version=self.client_version,
        )
        response = self._send(state, request)
        result = response.unwrap()
        ttl, scope = validate_cacheable_result(result.value)
        definitions = parse_tool_list(result.value)

        accepted, rejected = filter_tool_definitions(
            {
                definition.name: {
                    "name": definition.name,
                    "description": definition.description,
                    "inputSchema": definition.input_schema,
                }
                for definition in definitions
            }
        )
        state.tools = {
            definition.name: definition for definition in definitions if definition.name in accepted
        }
        state.definitions = accepted
        state.rejected_tools = rejected

        self._record(
            session_id,
            run_id,
            "mcp.tools_list",
            {
                "server": server,
                "accepted": sorted(state.tools),
                "rejected": rejected,
                "ttl_ms": ttl,
                "cache_scope": scope,
            },
        )
        for tool_name, reason in rejected.items():
            self._record(
                session_id,
                run_id,
                "mcp.tool_definition_rejected",
                {"server": server, "tool": tool_name, "reason": reason},
            )
        return tuple(state.tools.values())

    def _assert_enabled(self, server: str) -> None:
        if not self.enabled:
            raise MCPGatewayError(
                "MCP gateway is disabled; set MCP_ENABLED=true to use it. "
                "Disabling is the default so no remote server is contacted by accident."
            )
        state = self._require_server(server)
        if not state.config.enabled:
            raise MCPGatewayError(f"MCP server {server!r} is disabled")

    def _send(self, state: _ServerState, request: Any) -> MCPResponse:
        definition = None
        if request.method == METHOD_TOOLS_CALL:
            name = request.params.get("name")
            definition = state.definitions.get(str(name))
        return state.transport.send(
            request,
            tool_definition=definition,
            on_notification=lambda event: self._record(
                None, None, "mcp.notification", {"server": state.config.name, "event": event.event}
            ),
        )

    # -- governed tool call ----------------------------------------------

    def call_tool(
        self,
        server: str,
        tool: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        allowed_tools: Optional[Sequence[str]] = None,
        explicit_consent: bool = False,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> ToolCallOutcome:
        """Call a remote tool through scope, policy, reach and audit checks.

        Never raises for an expected refusal — the caller gets a
        :class:`ToolCallOutcome` whose ``status`` explains what happened.
        """
        started = time.perf_counter()

        def finish(status: str, *, result: Any = None, reason: str = "", detail: str = "") -> ToolCallOutcome:
            outcome = ToolCallOutcome(
                status=status,
                server=server,
                tool=tool,
                result=result,
                reason=reason,
                detail=detail,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return outcome

        namespaced = namespaced_tool_name(server, tool)

        # 1. Enablement and existence.
        try:
            self._assert_enabled(server)
            state = self._require_server(server)
        except MCPGatewayError as exc:
            self._record(
                session_id, run_id, "mcp.denied",
                {"server": server, "tool": namespaced, "stage": "enablement", "reason": str(exc)},
            )
            return finish("denied", reason=str(exc))

        # 2. Capability scope — declared by the server entry, narrowed by the
        #    active run's allowed tools.
        if not state.config.permits(tool):
            reason = f"tool {tool!r} is outside the configured scope for MCP server {server!r}"
            self._record(
                session_id, run_id, "mcp.denied",
                {"server": server, "tool": namespaced, "stage": "server_scope", "reason": reason},
            )
            return finish("denied", reason=reason)

        if allowed_tools is not None and tool not in set(allowed_tools):
            reason = f"tool {tool!r} is outside the active capability scope"
            self._record(
                session_id, run_id, "mcp.denied",
                {"server": server, "tool": namespaced, "stage": "run_scope", "reason": reason},
            )
            return finish("denied", reason=reason)

        # 3. Policy — the same engine the local tools go through.
        decision = self.policy.evaluate(
            tool_name=namespaced,
            declared_risk=state.config.declare_risk,
            allowed_tools=None,
            explicit_consent=explicit_consent,
        )
        risk = decision.risk.value if isinstance(decision.risk, ToolRisk) else str(decision.risk)
        if decision.decision is PolicyDecision.DENY:
            self._record(
                session_id, run_id, "mcp.denied",
                {"server": server, "tool": namespaced, "stage": "policy", "reason": decision.reason, "risk": risk},
            )
            return finish("denied", reason=decision.reason)
        if decision.decision is PolicyDecision.APPROVAL_REQUIRED:
            self._record(
                session_id, run_id, "mcp.approval_required",
                {"server": server, "tool": namespaced, "reason": decision.reason, "risk": risk},
            )
            return finish(
                "approval_required",
                reason=decision.reason or "explicit user approval is required",
            )

        # 4. Reach — re-validated per call so a DNS change cannot promote an
        #    endpoint into the private network after registration.
        try:
            self._assert_reachable(state.config)
        except Exception as exc:
            self._record(
                session_id, run_id, "mcp.denied",
                {"server": server, "tool": namespaced, "stage": "reach", "reason": str(exc)},
            )
            return finish("denied", reason=str(exc))

        # 5. The call itself.
        from ..tools.base import redact_payload

        payload = dict(arguments or {})
        self._record(
            session_id, run_id, "mcp.tool_call",
            {
                "server": server,
                "tool": namespaced,
                "risk": risk,
                "endpoint": state.transport.endpoint,
                "arguments": redact_payload(payload),
            },
        )

        request = build_request(
            self._next_id(),
            METHOD_TOOLS_CALL,
            client_name=self.client_name,
            client_version=self.client_version,
            params={"name": tool, "arguments": payload},
        )
        try:
            response = self._send(state, request)
        except (MCPTransportError, MCPLegacyServerError, MCPProtocolError) as exc:
            self._record(
                session_id, run_id, "mcp.error",
                {"server": server, "tool": namespaced, "error": type(exc).__name__},
            )
            return finish(
                "error",
                reason=f"MCP server {server!r} did not complete the request",
                detail=str(exc),
            )

        if response.error is not None:
            self._record(
                session_id, run_id, "mcp.error",
                {
                    "server": server,
                    "tool": namespaced,
                    "error_code": response.error.code,
                    "raw_error_code": response.error.raw_code,
                },
            )
            return finish(
                "error",
                reason=f"MCP server returned error {response.error.code}: {response.error.message}",
                detail=str(response.error.data) if response.error.data is not None else "",
            )

        result = response.unwrap()
        self._record(
            session_id, run_id, "mcp.tool_result",
            {
                "server": server,
                "tool": namespaced,
                "result_type": result.result_type,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        if not result.is_complete:
            # Multi Round-Trip Requests: the server wants more input and the
            # caller must retry the same request with inputResponses. Reported
            # rather than auto-answered — answering for the user would be the
            # gateway approving on their behalf.
            return finish(
                "approval_required",
                reason="server requires additional input before it can complete this request",
                detail=f"{len(result.input_requests)} input request(s)",
            )
        return finish("ok", result=result.value)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        for state in self._states.values():
            try:
                state.transport.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def __enter__(self) -> "MCPGateway":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def describe(self) -> list[dict[str, Any]]:
        """Operator-facing summary. Contains no credential material."""
        out: list[dict[str, Any]] = []
        for name, state in self._states.items():
            out.append(
                {
                    "name": name,
                    "endpoint": redact_headers({"url": state.transport.endpoint})["url"],
                    "transport": state.config.transport,
                    "enabled": state.config.enabled and self.enabled,
                    "declare_risk": state.config.declare_risk,
                    "scope": (
                        None
                        if state.config.allowed_tools is None
                        else sorted(state.config.allowed_tools)
                    ),
                    "credential": describe_credential(state.credential),
                    "tools": sorted(state.tools),
                    "rejected_tools": dict(state.rejected_tools),
                }
            )
        return out


__all__ = [
    "ALLOWED_TRANSPORTS",
    "MCPGateway",
    "MCPGatewayError",
    "MCPServerConfig",
    "NAMESPACE_PREFIX",
    "NAMESPACE_SEPARATOR",
    "ToolCallOutcome",
    "namespaced_tool_name",
    "servers_from_env",
    "split_namespaced_tool",
]
