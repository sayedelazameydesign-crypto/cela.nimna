"""MCP protocol contract — pure data, no I/O.

This module encodes the parts of the Model Context Protocol that the gateway
must not guess: the JSON-RPC envelope, the per-request ``_meta`` metadata
block, the error-code allocation, and the revision-specific rules that decide
whether a request may be routed at all.

Target revision: ``2026-07-28``.

That revision is *stateless*. Earlier revisions (``2025-03-26`` ..
``2025-11-25``) established a connection-scoped session through an
``initialize`` handshake and pinned it with an ``Mcp-Session-Id`` header. The
``2026-07-28`` revision removed both: every request carries its own protocol
version and client capabilities in ``_meta``, and list endpoints no longer vary
per connection.

Keeping this module free of transport and policy concerns is deliberate — it
lets the test suite assert the wire contract deterministically and offline,
which is the precondition the repository's *Definition of Done* sets for this
capability (``docs/architecture/agent-os-blueprint.md``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

# --------------------------------------------------------------------------
# Revision
# --------------------------------------------------------------------------

#: The revision this contract is written against.
PROTOCOL_VERSION = "2026-07-28"

#: Revisions that exist and that a counterpart may still speak. They are listed
#: so the gateway can name them in an ``UnsupportedProtocolVersionError``
#: instead of silently failing.
KNOWN_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

#: Revisions this contract can actually speak.
#:
#: Deliberately not the same as :data:`KNOWN_PROTOCOL_VERSIONS`: the older
#: revisions listed above exist, but implementing their ``initialize``
#: handshake and session header is separate work with its own tests.
#: Negotiation therefore offers only what is implemented, so a server that
#: speaks nothing newer is reported as unsupported instead of being half-used.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (PROTOCOL_VERSION,)

#: Revision that introduced the mirrored request-metadata headers. Revisions
#: below it cannot be trusted to carry headers that agree with the body, so an
#: intermediary must not route on them.
_HEADER_BEARING_MIN = (2025, 6, 18)

_VERSION_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def version_tuple(version: str) -> Optional[tuple[int, int, int]]:
    """Parse an MCP revision string into a comparable tuple, or ``None``."""
    match = _VERSION_RE.match(str(version or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_known_version(version: str) -> bool:
    return version_tuple(version) is not None


def version_carries_headers(version: str) -> bool:
    """True when the revision defines the mirrored request-metadata headers.

    Used by :mod:`nimna.mcp.headers` to refuse header-based policy decisions on
    revisions that never agreed headers with the body.
    """
    parsed = version_tuple(version)
    return parsed is not None and parsed >= _HEADER_BEARING_MIN


# --------------------------------------------------------------------------
# Method names
# --------------------------------------------------------------------------

#: ``server/discover`` is mandatory for servers on this revision. It advertises
#: supported protocol versions, capabilities and identity, and is the
#: up-front version-selection probe.
METHOD_DISCOVER = "server/discover"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"
METHOD_RESOURCES_LIST = "resources/list"
METHOD_RESOURCES_READ = "resources/read"
METHOD_RESOURCES_TEMPLATES_LIST = "resources/templates/list"
METHOD_PROMPTS_LIST = "prompts/list"
METHOD_PROMPTS_GET = "prompts/get"
METHOD_SUBSCRIPTIONS_LISTEN = "subscriptions/listen"

#: ``subscriptions/listen`` replaces the removed HTTP GET stream endpoint and
#: the removed ``resources/subscribe`` / ``resources/unsubscribe`` pair.
KNOWN_METHODS: frozenset[str] = frozenset(
    {
        METHOD_DISCOVER,
        METHOD_TOOLS_LIST,
        METHOD_TOOLS_CALL,
        METHOD_RESOURCES_LIST,
        METHOD_RESOURCES_READ,
        METHOD_RESOURCES_TEMPLATES_LIST,
        METHOD_PROMPTS_LIST,
        METHOD_PROMPTS_GET,
        METHOD_SUBSCRIPTIONS_LISTEN,
    }
)

#: Methods that carry ``params.name`` (or ``params.uri``) and therefore require
#: the ``Mcp-Name`` header.
NAME_BEARING_METHODS: frozenset[str] = frozenset(
    {METHOD_TOOLS_CALL, METHOD_RESOURCES_READ, METHOD_PROMPTS_GET}
)

#: Removed in this revision. Named so the gateway can explain a rejection
#: rather than reporting a generic "unknown method".
REMOVED_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "ping",
        "logging/setLevel",
        "notifications/roots/list_changed",
        "resources/subscribe",
        "resources/unsubscribe",
    }
)

#: ``resources/templates/list`` reads ``params.uri`` only when a concrete URI is
#: supplied; it is not a name-bearing method.

# --------------------------------------------------------------------------
# _meta keys
# --------------------------------------------------------------------------

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

#: Reserved keys an implementation must not mint on its own.
RESERVED_META_KEYS: frozenset[str] = frozenset(
    {
        META_PROTOCOL_VERSION,
        META_CLIENT_INFO,
        META_CLIENT_CAPABILITIES,
        META_SERVER_INFO,
        META_LOG_LEVEL,
        META_SUBSCRIPTION_ID,
    }
)

# --------------------------------------------------------------------------
# Error codes
# --------------------------------------------------------------------------
#
# The specification partitions the JSON-RPC server-error range:
#   -32000..-32019  implementation-defined (existing SDK usage grandfathered)
#   -32020..-32099  reserved for the MCP specification
#
# The three MCP-defined codes were renumbered in this revision; the old values
# are kept so a counterpart echoing them can be recognised instead of being
# reported as an unknown implementation error.

CODE_PARSE_ERROR = -32700
CODE_INVALID_REQUEST = -32600
CODE_METHOD_NOT_FOUND = -32601
CODE_INVALID_PARAMS = -32602
CODE_INTERNAL_ERROR = -32603

CODE_HEADER_MISMATCH = -32020
CODE_MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
CODE_UNSUPPORTED_PROTOCOL_VERSION = -32022

#: Pre-2026-07-28 spellings of the MCP-defined codes.
LEGACY_CODE_ALIASES: dict[int, int] = {
    -32001: CODE_HEADER_MISMATCH,
    -32003: CODE_MISSING_REQUIRED_CLIENT_CAPABILITY,
    -32004: CODE_UNSUPPORTED_PROTOCOL_VERSION,
}

MCP_DEFINED_CODES: frozenset[int] = frozenset(
    {CODE_HEADER_MISMATCH, CODE_MISSING_REQUIRED_CLIENT_CAPABILITY, CODE_UNSUPPORTED_PROTOCOL_VERSION}
)

#: Resource-not-found moved from ``-32002`` to ``-32602`` in this revision.
CODE_RESOURCE_NOT_FOUND_LEGACY = -32002


def normalize_error_code(code: Any) -> int:
    """Map a counterpart's error code onto this revision's allocation."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return CODE_INTERNAL_ERROR
    return LEGACY_CODE_ALIASES.get(value, value)


# --------------------------------------------------------------------------
# resultType
# --------------------------------------------------------------------------

RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_INPUT_REQUIRED = "input_required"

#: ``resultType`` became required in this revision. Results from earlier
#: servers omit it, and the specification requires clients to treat a missing
#: field as ``complete`` rather than as a protocol violation.
VALID_RESULT_TYPES: frozenset[str] = frozenset({RESULT_TYPE_COMPLETE, RESULT_TYPE_INPUT_REQUIRED})

#: Result fields required by the ``CacheableResult`` interface.
CACHEABLE_RESULT_FIELDS: tuple[str, ...] = ("ttlMs", "cacheScope")
VALID_CACHE_SCOPES: frozenset[str] = frozenset({"public", "private"})


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class MCPProtocolError(Exception):
    """The counterpart sent something that does not satisfy the contract."""

    def __init__(self, message: str, *, code: int = CODE_INVALID_REQUEST, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True)
class MCPError:
    """A JSON-RPC error object, with the code normalised to this revision."""

    code: int
    message: str
    data: Any = None
    #: The code as it appeared on the wire, before normalisation. Kept so that
    #: a legacy counterpart is visible in audit rather than silently rewritten.
    raw_code: Optional[int] = None

    @property
    def is_mcp_defined(self) -> bool:
        return self.code in MCP_DEFINED_CODES

    @property
    def supported_versions(self) -> tuple[str, ...]:
        """Versions advertised by an ``UnsupportedProtocolVersionError``."""
        if self.code != CODE_UNSUPPORTED_PROTOCOL_VERSION or not isinstance(self.data, Mapping):
            return ()
        listed = self.data.get("supported")
        if isinstance(listed, (list, tuple)):
            return tuple(str(item) for item in listed)
        return ()


@dataclass(frozen=True)
class MCPResult:
    """A successful JSON-RPC result.

    ``result_type`` is ``complete`` unless the counterpart explicitly asked for
    another round trip, which keeps results from earlier servers valid.
    """

    value: Any
    result_type: str = RESULT_TYPE_COMPLETE
    server_info: Optional[Mapping[str, Any]] = None

    @property
    def is_complete(self) -> bool:
        return self.result_type == RESULT_TYPE_COMPLETE

    @property
    def input_requests(self) -> tuple[Mapping[str, Any], ...]:
        """Input requests carried by an ``InputRequiredResult`` (MRTR)."""
        if self.result_type != RESULT_TYPE_INPUT_REQUIRED or not isinstance(self.value, Mapping):
            return ()
        listed = self.value.get("inputRequests")
        if isinstance(listed, (list, tuple)):
            return tuple(item for item in listed if isinstance(item, Mapping))
        return ()


@dataclass(frozen=True)
class MCPResponse:
    """A parsed JSON-RPC response: exactly one of ``result`` / ``error``."""

    id: Any
    result: Optional[MCPResult] = None
    error: Optional[MCPError] = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise MCPProtocolError("a JSON-RPC response must carry exactly one of result or error")

    @property
    def ok(self) -> bool:
        return self.error is None

    def unwrap(self) -> MCPResult:
        if self.error is not None:
            raise MCPProtocolError(
                f"MCP error {self.error.code}: {self.error.message}",
                code=self.error.code,
                data=self.error.data,
            )
        assert self.result is not None  # guaranteed by __post_init__
        return self.result


def build_meta(
    *,
    client_name: str,
    client_version: str,
    protocol_version: str = PROTOCOL_VERSION,
    client_capabilities: Optional[Mapping[str, Any]] = None,
    log_level: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the per-request ``_meta`` block.

    ``clientCapabilities`` is present even when empty: the specification makes
    it the mechanism by which a server decides whether a feature is available,
    and omitting it is indistinguishable from an empty capability set only by
    accident.
    """
    meta: dict[str, Any] = {
        META_PROTOCOL_VERSION: protocol_version,
        META_CLIENT_INFO: {"name": client_name, "version": client_version},
        META_CLIENT_CAPABILITIES: dict(client_capabilities or {}),
    }
    if log_level is not None:
        # Servers must not emit notifications/message unless the request opted
        # in by carrying this field, so it is only set when asked for.
        meta[META_LOG_LEVEL] = log_level
    if extra:
        collisions = RESERVED_META_KEYS.intersection(extra)
        if collisions:
            raise MCPProtocolError(
                "extra _meta must not override reserved keys: " + ", ".join(sorted(collisions))
            )
        meta.update(extra)
    return meta


@dataclass(frozen=True)
class MCPRequest:
    """A single JSON-RPC request, as this revision permits it to be sent."""

    id: Any
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": self.id,
            "method": self.method,
            "params": dict(self.params),
        }


def build_request(
    request_id: Any,
    method: str,
    *,
    client_name: str,
    client_version: str,
    params: Optional[Mapping[str, Any]] = None,
    protocol_version: str = PROTOCOL_VERSION,
    client_capabilities: Optional[Mapping[str, Any]] = None,
    log_level: Optional[str] = None,
    meta_extra: Optional[Mapping[str, Any]] = None,
) -> MCPRequest:
    """Assemble a request whose ``_meta`` satisfies the revision's contract.

    ``_meta`` is placed inside ``params`` because that is where every
    parameterised method carries it. ``server/discover`` is the exception: it
    takes no parameters, so callers that need metadata there should send the
    same payload as ``params`` — see :func:`build_discover_request`.
    """
    if not method or not isinstance(method, str):
        raise MCPProtocolError("method is required")
    if method not in KNOWN_METHODS:
        hint = " (removed in this revision)" if method in REMOVED_METHODS else ""
        raise MCPProtocolError(f"unsupported method '{method}'{hint}")

    payload = dict(params or {})
    if META_LOG_LEVEL in payload:
        raise MCPProtocolError(f"'{META_LOG_LEVEL}' must be supplied via log_level, not params")
    payload["_meta"] = build_meta(
        client_name=client_name,
        client_version=client_version,
        protocol_version=protocol_version,
        client_capabilities=client_capabilities,
        log_level=log_level,
        extra=meta_extra,
    )
    return MCPRequest(id=request_id, method=method, params=payload)


def build_discover_request(
    request_id: Any,
    *,
    client_name: str,
    client_version: str,
    protocol_version: str = PROTOCOL_VERSION,
    client_capabilities: Optional[Mapping[str, Any]] = None,
) -> MCPRequest:
    """``server/discover`` — mandatory on this revision.

    The metadata travels in a body-level ``_meta`` so that a server which
    advertises a different revision can still be understood.
    """
    return MCPRequest(
        id=request_id,
        method=METHOD_DISCOVER,
        params={
            "_meta": build_meta(
                client_name=client_name,
                client_version=client_version,
                protocol_version=protocol_version,
                client_capabilities=client_capabilities,
            )
        },
    )


def name_for_request(request: MCPRequest) -> Optional[str]:
    """The ``Mcp-Name`` source value for a request, or ``None``.

    ``params.name`` wins over ``params.uri``; the two are not both meaningful
    for any method this revision defines.
    """
    if request.method not in NAME_BEARING_METHODS:
        return None
    for key in ("name", "uri"):
        value = request.params.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def parse_response(payload: Any, *, request_id: Any = None) -> MCPResponse:
    """Parse and validate a JSON-RPC response body.

    Rejects a mismatched ``id`` — correlating the wrong response onto a request
    is the failure mode that makes a gateway unsafe under concurrency.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise MCPProtocolError(f"response is not valid JSON: {exc}", code=CODE_PARSE_ERROR) from exc
    if not isinstance(payload, Mapping):
        raise MCPProtocolError("response must be a JSON object")
    if payload.get("jsonrpc") != "2.0":
        raise MCPProtocolError("response must declare \"jsonrpc\": \"2.0\"")

    response_id = payload.get("id")
    if request_id is not None and response_id != request_id:
        raise MCPProtocolError(
            f"response id {response_id!r} does not match request id {request_id!r}"
        )

    has_result = "result" in payload
    has_error = "error" in payload
    if has_result == has_error:
        raise MCPProtocolError("response must carry exactly one of result or error")

    if has_error:
        raw = payload.get("error")
        if not isinstance(raw, Mapping):
            raise MCPProtocolError("error must be a JSON object")
        raw_code = raw.get("code")
        message = str(raw.get("message", ""))
        return MCPResponse(
            id=response_id,
            error=MCPError(
                code=normalize_error_code(raw_code),
                message=message,
                data=raw.get("data"),
                raw_code=raw_code if isinstance(raw_code, int) else None,
            ),
        )

    value = payload.get("result")
    result_type = RESULT_TYPE_COMPLETE
    server_info: Optional[Mapping[str, Any]] = None
    if isinstance(value, Mapping):
        declared = value.get("resultType")
        if declared is None:
            # Absent field is normal for pre-2026-07-28 servers and means
            # "complete" — never an error.
            result_type = RESULT_TYPE_COMPLETE
        elif declared in VALID_RESULT_TYPES:
            result_type = str(declared)
        else:
            raise MCPProtocolError(f"unknown resultType {declared!r}")
        meta = value.get("_meta")
        if isinstance(meta, Mapping):
            info = meta.get(META_SERVER_INFO)
            if isinstance(info, Mapping):
                server_info = info
    return MCPResponse(
        id=response_id, result=MCPResult(value=value, result_type=result_type, server_info=server_info)
    )


def parse_discover_result(value: Any) -> "DiscoveryResult":
    """Read a ``server/discover`` result."""
    if not isinstance(value, Mapping):
        raise MCPProtocolError("server/discover result must be a JSON object")
    versions = value.get("protocolVersions")
    if not isinstance(versions, (list, tuple)) or not versions:
        raise MCPProtocolError("server/discover result must advertise protocolVersions")
    server_info = value.get("serverInfo")
    capabilities = value.get("capabilities")
    return DiscoveryResult(
        protocol_versions=tuple(str(item) for item in versions),
        server_info=dict(server_info) if isinstance(server_info, Mapping) else {},
        capabilities=dict(capabilities) if isinstance(capabilities, Mapping) else {},
    )


@dataclass(frozen=True)
class DiscoveryResult:
    """What a server advertises about itself."""

    protocol_versions: tuple[str, ...]
    server_info: Mapping[str, Any] = field(default_factory=dict)
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    @property
    def supports_current_revision(self) -> bool:
        return PROTOCOL_VERSION in self.protocol_versions

    def negotiate(self, acceptable: Iterable[str] = SUPPORTED_PROTOCOL_VERSIONS) -> Optional[str]:
        """Highest mutually supported revision, or ``None``.

        Selection is by advertised preference order of this client, not by what
        the server happens to list first. The default offers only the revisions
        this contract implements, so a server that speaks nothing newer is
        reported as unsupported rather than silently negotiated down to a
        handshake that is not implemented.
        """
        offered = set(self.protocol_versions)
        for candidate in acceptable:
            if candidate in offered:
                return candidate
        return None


def unsupported_version_error(*, requested: str, supported: Iterable[str]) -> MCPError:
    """Build the ``UnsupportedProtocolVersionError`` this revision defines."""
    return MCPError(
        code=CODE_UNSUPPORTED_PROTOCOL_VERSION,
        message=f"unsupported protocol version {requested!r}",
        data={"requested": requested, "supported": list(supported)},
    )


@dataclass(frozen=True)
class ToolDefinition:
    """A tool as advertised by ``tools/list``."""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)


def parse_tool_list(value: Any) -> tuple[ToolDefinition, ...]:
    """Read a ``tools/list`` result into definitions."""
    if not isinstance(value, Mapping):
        raise MCPProtocolError("tools/list result must be a JSON object")
    raw_tools = value.get("tools")
    if raw_tools is None:
        return ()
    if not isinstance(raw_tools, (list, tuple)):
        raise MCPProtocolError("tools/list result 'tools' must be an array")

    tools: list[ToolDefinition] = []
    for entry in raw_tools:
        if not isinstance(entry, Mapping):
            raise MCPProtocolError("each tool definition must be a JSON object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise MCPProtocolError("tool definition requires a non-empty name")
        schema = entry.get("inputSchema")
        tools.append(
            ToolDefinition(
                name=name,
                description=str(entry.get("description", "")),
                input_schema=dict(schema) if isinstance(schema, Mapping) else {},
            )
        )
    return tuple(tools)


def validate_cacheable_result(value: Any) -> tuple[Optional[int], Optional[str]]:
    """Read ``ttlMs`` / ``cacheScope`` from a list result.

    Absent fields are returned as ``None`` — earlier servers do not send them,
    and the specification only makes them required for servers on this
    revision. A present-but-invalid value is a contract violation.
    """
    if not isinstance(value, Mapping):
        return None, None
    ttl = value.get("ttlMs")
    scope = value.get("cacheScope")
    parsed_ttl: Optional[int] = None
    if ttl is not None:
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise MCPProtocolError("ttlMs must be a number of milliseconds")
        parsed_ttl = int(ttl)
        if parsed_ttl < 0:
            raise MCPProtocolError("ttlMs must not be negative")
    parsed_scope: Optional[str] = None
    if scope is not None:
        if scope not in VALID_CACHE_SCOPES:
            raise MCPProtocolError(f"cacheScope must be one of {sorted(VALID_CACHE_SCOPES)}")
        parsed_scope = str(scope)
    return parsed_ttl, parsed_scope


__all__ = [
    "CACHEABLE_RESULT_FIELDS",
    "CODE_HEADER_MISMATCH",
    "CODE_INTERNAL_ERROR",
    "CODE_INVALID_PARAMS",
    "CODE_INVALID_REQUEST",
    "CODE_METHOD_NOT_FOUND",
    "CODE_MISSING_REQUIRED_CLIENT_CAPABILITY",
    "CODE_PARSE_ERROR",
    "CODE_RESOURCE_NOT_FOUND_LEGACY",
    "CODE_UNSUPPORTED_PROTOCOL_VERSION",
    "DiscoveryResult",
    "KNOWN_METHODS",
    "KNOWN_PROTOCOL_VERSIONS",
    "LEGACY_CODE_ALIASES",
    "MCP_DEFINED_CODES",
    "MCPError",
    "MCPProtocolError",
    "MCPRequest",
    "MCPResponse",
    "MCPResult",
    "META_CLIENT_CAPABILITIES",
    "META_CLIENT_INFO",
    "META_LOG_LEVEL",
    "META_PROTOCOL_VERSION",
    "META_SERVER_INFO",
    "META_SUBSCRIPTION_ID",
    "METHOD_DISCOVER",
    "METHOD_PROMPTS_GET",
    "METHOD_PROMPTS_LIST",
    "METHOD_RESOURCES_LIST",
    "METHOD_RESOURCES_READ",
    "METHOD_RESOURCES_TEMPLATES_LIST",
    "METHOD_SUBSCRIPTIONS_LISTEN",
    "METHOD_TOOLS_CALL",
    "METHOD_TOOLS_LIST",
    "NAME_BEARING_METHODS",
    "PROTOCOL_VERSION",
    "REMOVED_METHODS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "RESERVED_META_KEYS",
    "RESULT_TYPE_COMPLETE",
    "RESULT_TYPE_INPUT_REQUIRED",
    "ToolDefinition",
    "VALID_CACHE_SCOPES",
    "VALID_RESULT_TYPES",
    "build_discover_request",
    "build_meta",
    "build_request",
    "is_known_version",
    "name_for_request",
    "normalize_error_code",
    "parse_discover_result",
    "parse_response",
    "parse_tool_list",
    "unsupported_version_error",
    "validate_cacheable_result",
    "version_carries_headers",
    "version_tuple",
]
