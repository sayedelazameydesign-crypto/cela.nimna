"""Governed MCP gateway — contract, headers, transport and policy.

Targets MCP protocol revision ``2026-07-28`` (the stateless revision). The
contract layer is split from the transport and the policy layer so each can be
tested offline and reasoned about on its own:

* :mod:`nimna.mcp.contract` — envelope, ``_meta``, error allocation, versioning
* :mod:`nimna.mcp.headers`  — mirrored request headers and header/body agreement
* :mod:`nimna.mcp.auth`     — credentials that resist being logged
* :mod:`nimna.mcp.transport`— Streamable HTTP binding
* :mod:`nimna.mcp.gateway`  — scope, policy, reach checks and audit
"""
from .auth import (
    MCPAuthError,
    MCPCredential,
    credential_from_env,
    describe_credential,
    redact_headers,
    require_credential,
)
from .contract import (
    CODE_HEADER_MISMATCH,
    CODE_MISSING_REQUIRED_CLIENT_CAPABILITY,
    CODE_UNSUPPORTED_PROTOCOL_VERSION,
    KNOWN_PROTOCOL_VERSIONS,
    MCPError,
    MCPProtocolError,
    MCPRequest,
    MCPResponse,
    MCPResult,
    PROTOCOL_VERSION,
    DiscoveryResult,
    ToolDefinition,
    build_discover_request,
    build_request,
    parse_discover_result,
    parse_response,
    parse_tool_list,
    unsupported_version_error,
    version_carries_headers,
)
from .gateway import (
    MCPGateway,
    MCPGatewayError,
    MCPServerConfig,
    ToolCallOutcome,
    namespaced_tool_name,
    servers_from_env,
    split_namespaced_tool,
)
from .headers import (
    HEADER_METHOD,
    HEADER_NAME,
    HEADER_PROTOCOL_VERSION,
    HeaderContractError,
    InvalidToolDefinitionError,
    check_header_body_agreement,
    decode_header_value,
    derive_headers,
    encode_header_value,
    extract_param_headers,
    filter_tool_definitions,
    header_params_from_schema,
)
from .transport import (
    MCPLegacyServerError,
    MCPStreamTruncatedError,
    MCPTimeoutError,
    MCPTransport,
    MCPTransportError,
    SSEEvent,
    StreamableHTTPTransport,
    iter_sse_events,
)

__all__ = [
    "CODE_HEADER_MISMATCH",
    "CODE_MISSING_REQUIRED_CLIENT_CAPABILITY",
    "CODE_UNSUPPORTED_PROTOCOL_VERSION",
    "DiscoveryResult",
    "HEADER_METHOD",
    "HEADER_NAME",
    "HEADER_PROTOCOL_VERSION",
    "HeaderContractError",
    "InvalidToolDefinitionError",
    "KNOWN_PROTOCOL_VERSIONS",
    "MCPAuthError",
    "MCPCredential",
    "MCPError",
    "MCPGateway",
    "MCPGatewayError",
    "MCPLegacyServerError",
    "MCPProtocolError",
    "MCPRequest",
    "MCPResponse",
    "MCPResult",
    "MCPServerConfig",
    "MCPStreamTruncatedError",
    "MCPTimeoutError",
    "MCPTransport",
    "MCPTransportError",
    "PROTOCOL_VERSION",
    "SSEEvent",
    "StreamableHTTPTransport",
    "ToolCallOutcome",
    "ToolDefinition",
    "build_discover_request",
    "build_request",
    "check_header_body_agreement",
    "credential_from_env",
    "decode_header_value",
    "derive_headers",
    "describe_credential",
    "encode_header_value",
    "extract_param_headers",
    "filter_tool_definitions",
    "header_params_from_schema",
    "iter_sse_events",
    "namespaced_tool_name",
    "parse_discover_result",
    "parse_response",
    "parse_tool_list",
    "redact_headers",
    "servers_from_env",
    "require_credential",
    "split_namespaced_tool",
    "unsupported_version_error",
    "version_carries_headers",
]
