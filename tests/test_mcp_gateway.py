"""Offline contract tests for the governed MCP gateway.

Everything here runs without a network and without an MCP server. The transport
is driven through ``httpx.MockTransport``, and the gateway's reachability check
is relaxed only where the test is about something else — the SSRF gate itself
has its own tests at the bottom.

The tests are grouped by the boundary they pin, because each boundary is a
separate claim:

* the JSON-RPC / ``_meta`` contract
* mirrored request headers, including header/body agreement
* the Streamable HTTP binding as this revision defines it
* gateway scope, policy, audit and fail-closed behaviour
"""
from __future__ import annotations

import json

import httpx
import pytest

from nimna.evidence.journal import EvidenceJournal
from nimna.mcp import (
    HEADER_METHOD,
    HEADER_NAME,
    HEADER_PROTOCOL_VERSION,
    CODE_HEADER_MISMATCH,
    CODE_UNSUPPORTED_PROTOCOL_VERSION,
    MCPAuthError,
    MCPCredential,
    MCPGateway,
    MCPGatewayError,
    MCPLegacyServerError,
    MCPProtocolError,
    MCPServerConfig,
    MCPStreamTruncatedError,
    MCPTimeoutError,
    PROTOCOL_VERSION,
    StreamableHTTPTransport,
    build_request,
    check_header_body_agreement,
    decode_header_value,
    derive_headers,
    encode_header_value,
    filter_tool_definitions,
    header_params_from_schema,
    iter_sse_events,
    namespaced_tool_name,
    parse_discover_result,
    parse_response,
    parse_tool_list,
    split_namespaced_tool,
    version_carries_headers,
)
from nimna.mcp.contract import validate_cacheable_result
from nimna.mcp.headers import (
    HeaderContractError,
    InvalidToolDefinitionError,
    assert_routable_version,
    extract_param_headers,
)
from nimna.memory import MemoryStore
from nimna.tools.builtin.web import assert_public_url

PUBLIC_ENDPOINT = "https://mcp.example.com/mcp"


# ==========================================================================
# Helpers
# ==========================================================================


def json_response(payload, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def sse_response(events: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "text/event-stream"}, content=events.encode("utf-8")
    )


def make_transport(handler, *, credential=None, url: str = PUBLIC_ENDPOINT) -> StreamableHTTPTransport:
    return StreamableHTTPTransport(
        url,
        credential=credential,
        http_client=httpx.Client(base_url=url, transport=httpx.MockTransport(handler)),
    )


def echo_result(result: dict, *, status: int = 200):
    """A handler that answers with ``result``, echoing the caller's request id.

    Echoing matters: a fixed id makes every call after the first look like a
    response-correlation failure, which is a different (real) bug than the one
    a test is usually trying to pin.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        rid = json.loads(request.content)["id"]
        return json_response({"jsonrpc": "2.0", "id": rid, "result": result}, status=status)

    return handler


def make_gateway(handler, **overrides) -> MCPGateway:
    """Gateway wired to a mock endpoint.

    ``allow_private_networks`` keeps the reachability check out of the way: it
    has dedicated tests and it would otherwise make every request here depend
    on DNS.
    """
    fields = {"name": "demo", "url": PUBLIC_ENDPOINT, "require_auth": False, "declare_risk": "safe"}
    fields.update(overrides)
    config = MCPServerConfig(**fields)
    return MCPGateway(
        [config],
        enabled=True,
        allow_private_networks=True,
        transport_factory=lambda _config, credential: make_transport(handler, credential=credential),
    )


TOOLS_LIST_RESULT = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "resultType": "complete",
        "ttlMs": 30000,
        "cacheScope": "private",
        "tools": [
            {
                "name": "get_weather",
                "description": "Look up weather",
                "inputSchema": {"type": "object", "properties": {"location": {"type": "string"}}},
            }
        ],
    },
}


# ==========================================================================
# Contract: envelope, _meta, errors, resultType
# ==========================================================================


def test_targets_the_stateless_revision_and_has_no_session_concept():
    from nimna.mcp import contract

    assert contract.PROTOCOL_VERSION == "2026-07-28"
    # Sessions and the initialize handshake were removed in this revision.
    assert "initialize" in contract.REMOVED_METHODS
    assert "notifications/initialized" in contract.REMOVED_METHODS
    assert not hasattr(contract, "HEADER_SESSION_ID")
    assert "Mcp-Session-Id" not in {name for name in dir(contract)}


def test_build_request_carries_version_and_capabilities_in_meta():
    request = build_request(
        7,
        "tools/list",
        client_name="nimna",
        client_version="0.1.0",
        client_capabilities={"elicitation": {}},
    )
    assert request.to_wire()["jsonrpc"] == "2.0"
    meta = request.params["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == PROTOCOL_VERSION
    assert meta["io.modelcontextprotocol/clientInfo"] == {"name": "nimna", "version": "0.1.0"}
    # Present even when empty, so a server can tell "no capabilities" from
    # "the client forgot to say".
    assert meta["io.modelcontextprotocol/clientCapabilities"] == {"elicitation": {}}


def test_build_request_rejects_unknown_and_removed_methods():
    with pytest.raises(MCPProtocolError, match="unsupported method"):
        build_request(1, "tools/execute", client_name="nimna", client_version="0.1.0")
    with pytest.raises(MCPProtocolError, match="removed in this revision"):
        build_request(1, "initialize", client_name="nimna", client_version="0.1.0")


def test_build_request_refuses_log_level_smuggled_through_params():
    with pytest.raises(MCPProtocolError, match="log_level"):
        build_request(
            1,
            "tools/list",
            client_name="nimna",
            client_version="0.1.0",
            params={"io.modelcontextprotocol/logLevel": "debug"},
        )


def test_parse_response_rejects_id_mismatch():
    payload = {"jsonrpc": "2.0", "id": 99, "result": {"resultType": "complete"}}
    with pytest.raises(MCPProtocolError, match="does not match request id"):
        parse_response(payload, request_id=1)


def test_parse_response_requires_exactly_one_of_result_or_error():
    with pytest.raises(MCPProtocolError, match="exactly one"):
        parse_response({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": -1, "message": "x"}})
    with pytest.raises(MCPProtocolError, match="exactly one"):
        parse_response({"jsonrpc": "2.0", "id": 1})


def test_result_without_result_type_is_complete_not_an_error():
    """Results from pre-2026-07-28 servers omit resultType and MUST be accepted."""
    parsed = parse_response({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    assert parsed.ok
    assert parsed.unwrap().result_type == "complete"
    assert parsed.unwrap().is_complete


def test_input_required_result_exposes_input_requests():
    parsed = parse_response(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "resultType": "input_required",
                "inputRequests": [{"type": "elicitation", "message": "pick a region"}],
            },
        }
    )
    result = parsed.unwrap()
    assert not result.is_complete
    assert len(result.input_requests) == 1
    assert result.input_requests[0]["type"] == "elicitation"


def test_unknown_result_type_is_rejected():
    with pytest.raises(MCPProtocolError, match="unknown resultType"):
        parse_response({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "partial"}})


def test_legacy_error_codes_are_normalised_and_the_wire_value_kept():
    parsed = parse_response(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32004, "message": "unsupported version"}}
    )
    error = parsed.error
    assert error.code == CODE_UNSUPPORTED_PROTOCOL_VERSION
    assert error.raw_code == -32004
    assert error.is_mcp_defined


def test_unsupported_version_error_advertises_supported_versions():
    from nimna.mcp import unsupported_version_error

    error = unsupported_version_error(requested="2024-01-01", supported=["2026-07-28"])
    assert error.code == CODE_UNSUPPORTED_PROTOCOL_VERSION
    assert error.supported_versions == ("2026-07-28",)


def test_server_info_is_read_from_result_meta():
    parsed = parse_response(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "resultType": "complete",
                "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "srv", "version": "1"}},
            },
        }
    )
    assert parsed.unwrap().server_info == {"name": "srv", "version": "1"}


def test_discover_result_negotiates_by_client_preference():
    discovery = parse_discover_result(
        {
            "protocolVersions": ["2025-06-18", "2026-07-28"],
            "serverInfo": {"name": "srv", "version": "2"},
            "capabilities": {"tools": {}},
        }
    )
    assert discovery.supports_current_revision
    assert discovery.negotiate() == PROTOCOL_VERSION
    # A server offering only older revisions must not be silently adopted.
    older = parse_discover_result({"protocolVersions": ["2024-11-05"]})
    assert not older.supports_current_revision
    assert older.negotiate() is None


def test_discover_requires_advertised_versions():
    with pytest.raises(MCPProtocolError, match="protocolVersions"):
        parse_discover_result({"serverInfo": {}})


def test_cacheable_result_fields_are_validated_when_present():
    assert validate_cacheable_result({"ttlMs": 5000, "cacheScope": "public"}) == (5000, "public")
    # Absent is fine for older servers.
    assert validate_cacheable_result({"tools": []}) == (None, None)
    with pytest.raises(MCPProtocolError, match="cacheScope"):
        validate_cacheable_result({"cacheScope": "shared"})
    with pytest.raises(MCPProtocolError, match="negative"):
        validate_cacheable_result({"ttlMs": -1})


def test_subscription_notification_metadata_key_is_reserved():
    from nimna.mcp.contract import META_SUBSCRIPTION_ID, build_meta

    with pytest.raises(MCPProtocolError, match="reserved keys"):
        build_meta(
            client_name="nimna",
            client_version="0.1.0",
            extra={META_SUBSCRIPTION_ID: "forged"},
        )


def test_parse_tool_list_shape():
    tools = parse_tool_list(TOOLS_LIST_RESULT["result"])
    assert [tool.name for tool in tools] == ["get_weather"]
    with pytest.raises(MCPProtocolError, match="non-empty name"):
        parse_tool_list({"tools": [{"description": "no name"}]})


# ==========================================================================
# Headers: encoding
# ==========================================================================


@pytest.mark.parametrize(
    "raw",
    [
        "Hello, 世界",
        " padded ",
        "line1\nline2",
        "=?base64?literal?=",
        "",
        "ctrl\x07char",
    ],
)
def test_unsafe_values_are_base64_encoded_and_round_trip(raw):
    encoded = encode_header_value(raw)
    assert encoded.startswith("=?base64?") and encoded.endswith("?=")
    assert decode_header_value(encoded) == raw


def test_plain_values_stay_plain():
    for raw in ("us-west1", "get_weather", "file:///projects/myapp/config.json", "a b"):
        assert encode_header_value(raw) == raw


def test_internal_tab_and_space_are_header_safe_but_edge_whitespace_is_not():
    # RFC 9110 permits tab and space inside a field value; only leading and
    # trailing whitespace is ambiguous after trimming.
    assert encode_header_value("a\tb") == "a\tb"
    assert encode_header_value(" padded") != " padded"
    assert encode_header_value("padded ") != "padded "


def test_header_injection_is_impossible_through_encoding():
    """CR/LF must never reach a header as literal characters."""
    encoded = encode_header_value("evil\r\nX-Injected: 1")
    assert "\r" not in encoded and "\n" not in encoded
    assert decode_header_value(encoded) == "evil\r\nX-Injected: 1"


def test_malformed_base64_sentinel_is_rejected_not_ignored():
    with pytest.raises(HeaderContractError, match="malformed base64"):
        decode_header_value("=?base64?not!valid!base64?=")


def test_empty_string_is_distinguishable_from_an_omitted_header():
    assert encode_header_value("") != ""


# ==========================================================================
# Headers: derivation and agreement
# ==========================================================================


def test_derive_headers_sets_the_three_required_headers():
    request = build_request(
        1,
        "tools/call",
        client_name="nimna",
        client_version="0.1.0",
        params={"name": "get_weather", "arguments": {"location": "Cairo"}},
    )
    headers = derive_headers(request)
    assert headers[HEADER_PROTOCOL_VERSION] == PROTOCOL_VERSION
    assert headers[HEADER_METHOD] == "tools/call"
    assert headers[HEADER_NAME] == "get_weather"
    assert headers["Accept"] == "application/json, text/event-stream"


def test_derive_headers_omits_name_for_non_name_bearing_methods():
    request = build_request(1, "tools/list", client_name="nimna", client_version="0.1.0")
    assert HEADER_NAME not in derive_headers(request)


def test_tools_call_without_a_name_is_refused():
    request = build_request(1, "tools/call", client_name="nimna", client_version="0.1.0", params={})
    with pytest.raises(HeaderContractError, match="requires a non-empty params.name"):
        derive_headers(request)


def test_non_ascii_tool_name_is_encoded_on_the_wire():
    request = build_request(
        1,
        "tools/call",
        client_name="nimna",
        client_version="0.1.0",
        params={"name": "طقس", "arguments": {}},
    )
    headers = derive_headers(request)
    assert headers[HEADER_NAME].startswith("=?base64?")
    assert decode_header_value(headers[HEADER_NAME]) == "طقس"


def test_revisions_without_header_validation_must_not_use_headers():
    request = build_request(
        1,
        "tools/list",
        client_name="nimna",
        client_version="0.1.0",
        protocol_version="2025-03-26",
    )
    assert version_carries_headers("2025-03-26") is False
    with pytest.raises(HeaderContractError, match="does not define mirrored request headers"):
        derive_headers(request, protocol_version="2025-03-26")


def test_header_and_body_version_must_agree_at_derivation():
    request = build_request(1, "tools/list", client_name="nimna", client_version="0.1.0")
    with pytest.raises(HeaderContractError, match="disagrees"):
        derive_headers(request, protocol_version="2025-11-25")


def test_header_body_agreement_catches_a_split_brain_request():
    """The confused-deputy case: router reads the header, server runs the body."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_weather",
            "arguments": {},
            "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION},
        },
    }
    good = {
        HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION,
        HEADER_METHOD: "tools/call",
        HEADER_NAME: "get_weather",
    }
    check_header_body_agreement(good, body)

    mismatched = dict(good, **{HEADER_NAME: "delete_everything"})
    with pytest.raises(HeaderContractError, match="Header mismatch"):
        check_header_body_agreement(mismatched, body)

    with pytest.raises(HeaderContractError, match="missing required header"):
        check_header_body_agreement({HEADER_METHOD: "tools/call"}, body)

    with pytest.raises(HeaderContractError, match="present but the body carries no name"):
        check_header_body_agreement(dict(good, **{HEADER_NAME: "extra"}), {**body, "params": {"_meta": body["params"]["_meta"]}})


def test_intermediary_refuses_to_route_on_unvalidated_header_versions():
    assert assert_routable_version({HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION}) == PROTOCOL_VERSION
    with pytest.raises(HeaderContractError, match="without MCP-Protocol-Version"):
        assert_routable_version({})
    with pytest.raises(HeaderContractError, match="does not require header/body validation"):
        assert_routable_version({HEADER_PROTOCOL_VERSION: "2025-03-26"})


# ==========================================================================
# Headers: x-mcp-header tool definitions
# ==========================================================================

VALID_SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string", "x-mcp-header": "Region"},
        "retries": {"type": "integer", "x-mcp-header": "Retries"},
        "query": {"type": "string"},
    },
    "required": ["region", "query"],
}


def test_valid_x_mcp_header_schema_is_accepted():
    params = header_params_from_schema(VALID_SCHEMA)
    assert {param.header_name for param in params} == {"Mcp-Param-Region", "Mcp-Param-Retries"}


@pytest.mark.parametrize(
    "schema, match",
    [
        ({"properties": {"a": {"type": "number", "x-mcp-header": "A"}}}, "requires primitive type"),
        ({"properties": {"a": {"type": "string", "x-mcp-header": ""}}}, "must not be empty"),
        ({"properties": {"a": {"type": "string", "x-mcp-header": "Bad Name"}}}, "not a valid HTTP field name token"),
        ({"properties": {"a": {"type": "string", "x-mcp-header": "A\nB"}}}, "not a valid HTTP field name token"),
        (
            {
                "properties": {
                    "a": {"type": "string", "x-mcp-header": "Dup"},
                    "b": {"type": "string", "x-mcp-header": "dup"},
                }
            },
            "duplicate x-mcp-header",
        ),
        ({"properties": {"a": {"type": "array", "items": {"type": "string", "x-mcp-header": "A"}}}}, "statically reachable"),
        ({"properties": {"a": {"oneOf": [{"type": "string", "x-mcp-header": "A"}]}}}, "statically reachable"),
    ],
)
def test_invalid_x_mcp_header_schemas_are_rejected(schema, match):
    with pytest.raises(InvalidToolDefinitionError, match=match):
        header_params_from_schema(schema)


def test_nested_properties_path_is_accepted():
    params = header_params_from_schema(
        {
            "type": "object",
            "properties": {
                "opts": {
                    "type": "object",
                    "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
                }
            },
        }
    )
    assert params[0].path == ("opts", "region")


def test_one_bad_tool_definition_does_not_remove_the_valid_ones():
    accepted, rejected = filter_tool_definitions(
        {
            "good": {"inputSchema": VALID_SCHEMA},
            "bad": {"inputSchema": {"properties": {"a": {"type": "number", "x-mcp-header": "A"}}}},
        }
    )
    assert set(accepted) == {"good"}
    assert set(rejected) == {"bad"}
    assert "primitive type" in rejected["bad"]


def test_extract_param_headers_omits_absent_and_null_values():
    assert extract_param_headers(VALID_SCHEMA, {"region": "us-west1"}) == {
        "Mcp-Param-Region": "us-west1"
    }
    assert extract_param_headers(VALID_SCHEMA, {"region": None}) == {}
    assert extract_param_headers(VALID_SCHEMA, {}) == {}


def test_extract_param_headers_converts_primitives_per_the_rules():
    headers = extract_param_headers(VALID_SCHEMA, {"region": "us-west1", "retries": 3})
    assert headers["Mcp-Param-Retries"] == "3"

    bool_schema = {"properties": {"flag": {"type": "boolean", "x-mcp-header": "Flag"}}}
    assert extract_param_headers(bool_schema, {"flag": True}) == {"Mcp-Param-Flag": "true"}
    assert extract_param_headers(bool_schema, {"flag": False}) == {"Mcp-Param-Flag": "false"}


def test_extract_param_headers_rejects_type_and_range_violations():
    with pytest.raises(HeaderContractError, match="declared integer"):
        extract_param_headers(VALID_SCHEMA, {"retries": True})
    with pytest.raises(HeaderContractError, match="safe integer range"):
        extract_param_headers(VALID_SCHEMA, {"retries": 2**53})
    with pytest.raises(HeaderContractError, match="declared string"):
        extract_param_headers(VALID_SCHEMA, {"region": 5})


# ==========================================================================
# Transport: SSE framing
# ==========================================================================


def test_sse_parsing_handles_framing_and_ignores_comments():
    body = (
        ": keep-alive\n\n"
        "event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n"
        "id: 42\ndata: line one\ndata: line two\n\n"
    )
    events = list(iter_sse_events(iter(body.splitlines())))
    assert len(events) == 2
    assert events[0].event == "message"
    assert json.loads(events[0].data)["method"] == "notifications/progress"
    assert events[1].id == "42"
    assert events[1].data == "line one\nline two"


# ==========================================================================
# Transport: Streamable HTTP binding
# ==========================================================================


def test_json_response_is_returned_and_headers_are_correct():
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return json_response({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete", "tools": []}})

    with make_transport(handler) as transport:
        response = transport.send(
            build_request(1, "tools/list", client_name="nimna", client_version="0.1.0")
        )
    assert response.ok
    headers = seen[0]
    assert headers[HEADER_PROTOCOL_VERSION.lower()] == PROTOCOL_VERSION
    assert headers[HEADER_METHOD.lower()] == "tools/list"
    assert headers["accept"] == "application/json, text/event-stream"
    # No session machinery and no handshake on a stateless revision.
    assert "mcp-session-id" not in headers
    assert "last-event-id" not in headers
    assert json.loads(seen and "{}" or "{}") == {}


def test_no_initialize_handshake_is_sent_for_a_tools_list():
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(json.loads(request.content)["method"])
        return json_response({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete"}})

    with make_transport(handler) as transport:
        transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))
    assert methods == ["tools/list"]


def test_sse_stream_returns_final_response_and_surfaces_notifications():
    body = (
        "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{\"progress\":1}}\n\n"
        "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"resultType\":\"complete\",\"tools\":[]}}\n\n"
    )
    notifications = []

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(body)

    with make_transport(handler) as transport:
        response = transport.send(
            build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"),
            on_notification=notifications.append,
        )
    assert response.ok
    assert len(notifications) == 1
    assert notifications[0].event is None


def test_sse_stream_that_ends_without_a_response_is_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response("data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n")

    with make_transport(handler) as transport:
        with pytest.raises(MCPStreamTruncatedError, match="not resumable"):
            transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))


def test_202_for_a_request_is_refused():
    """202 is legal only for notifications; accepting it would drop the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    with make_transport(handler) as transport:
        with pytest.raises(MCPProtocolError, match="202"):
            transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))


def test_400_with_a_recognised_modern_error_is_not_treated_as_legacy():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": CODE_HEADER_MISMATCH, "message": "Header mismatch: Mcp-Name"},
            },
            status=400,
        )

    with make_transport(handler) as transport:
        response = transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))
    assert response.error is not None
    assert response.error.code == CODE_HEADER_MISMATCH


def test_400_without_a_modern_error_body_reports_a_legacy_server():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"")

    with make_transport(handler) as transport:
        with pytest.raises(MCPLegacyServerError, match="initialize-based revisions are not implemented"):
            transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))


def test_404_method_not_found_is_distinguished_from_a_legacy_endpoint():
    def not_implemented(request: httpx.Request) -> httpx.Response:
        return json_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}},
            status=404,
        )

    def legacy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"Not Found")

    # A 404 carrying a JSON-RPC error is a real modern endpoint that does not
    # implement the method, and is reported as an error response.
    with make_transport(not_implemented) as transport:
        response = transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))
    assert response.error is not None
    assert response.error.code == -32601

    # A bare 404 is a different condition: no modern endpoint there at all.
    with make_transport(legacy) as transport:
        with pytest.raises(MCPLegacyServerError, match="legacy HTTP\\+SSE"):
            transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))


def test_timeout_is_reported_without_claiming_the_work_stopped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with make_transport(handler) as transport:
        with pytest.raises(MCPTimeoutError, match="may still be working"):
            transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))


def test_oversized_response_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"jsonrpc": "2.0", "id": 1, "result": {"blob": "x" * 5000}})

    transport = StreamableHTTPTransport(
        PUBLIC_ENDPOINT,
        max_response_bytes=512,
        http_client=httpx.Client(base_url=PUBLIC_ENDPOINT, transport=httpx.MockTransport(handler)),
    )
    with transport:
        with pytest.raises(Exception, match="exceeded"):
            transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))


def test_endpoint_strips_embedded_userinfo_before_anything_can_log_it():
    transport = StreamableHTTPTransport("https://user:supersecret@mcp.example.com/mcp")
    try:
        assert "supersecret" not in transport.endpoint
        assert transport.endpoint == "https://mcp.example.com/mcp"
    finally:
        transport.close()


def test_credential_is_applied_but_never_rendered():
    seen: list[httpx.Headers] = []
    credential = MCPCredential(value="super-secret-token")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return json_response({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete"}})

    with make_transport(handler, credential=credential) as transport:
        transport.send(build_request(1, "tools/list", client_name="nimna", client_version="0.1.0"))
    assert seen[0]["authorization"] == "Bearer super-secret-token"
    assert "super-secret-token" not in repr(credential)
    assert "super-secret-token" not in str(credential)


def test_credential_with_crlf_is_refused_at_construction():
    with pytest.raises(MCPAuthError, match="CR or LF"):
        MCPCredential(value="token\r\nX-Injected: 1")


def test_param_headers_are_sent_for_a_tools_call():
    seen: list[httpx.Headers] = []
    definition = {"name": "execute_sql", "inputSchema": VALID_SCHEMA}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return json_response({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete"}})

    request = build_request(
        1,
        "tools/call",
        client_name="nimna",
        client_version="0.1.0",
        params={"name": "execute_sql", "arguments": {"region": "us-west1", "retries": 2}},
    )
    with make_transport(handler) as transport:
        transport.send(request, tool_definition=definition)
    assert seen[0]["mcp-param-region"] == "us-west1"
    assert seen[0]["mcp-param-retries"] == "2"


# ==========================================================================
# Gateway: configuration, scope, policy, fail-closed
# ==========================================================================


def test_gateway_is_disabled_by_default():
    gateway = MCPGateway([], enabled=False)
    assert gateway.enabled is False
    outcome = gateway.call_tool("demo", "get_weather")
    assert outcome.status == "denied"
    assert "disabled" in outcome.reason


def test_from_settings_honours_the_disabled_default():
    from nimna.config import Settings

    settings = Settings.from_env(env_file=None)
    assert settings.mcp_enabled is False
    assert settings.mcp_allow_private_networks is False
    gateway = MCPGateway.from_settings(settings, [])
    assert gateway.enabled is False


def test_unknown_server_is_denied_and_recorded():
    store = MemoryStore(":memory:")
    gateway = MCPGateway([], enabled=True, evidence=EvidenceJournal(store))
    outcome = gateway.call_tool("nope", "tool", session_id="s", run_id="r")
    assert outcome.status == "denied"
    events = [row["event"] for row in store.get_audit(run_id="r")]
    assert "mcp.denied" in events


def test_private_endpoint_is_refused_at_construction():
    with pytest.raises(Exception, match="local/internal|private"):
        MCPGateway([MCPServerConfig(name="local", url="http://127.0.0.1:9/mcp", require_auth=False)], enabled=True)


def test_tool_outside_the_configured_scope_is_denied_before_any_request():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return json_response({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete"}})

    gateway = make_gateway(handler, allowed_tools=frozenset({"get_weather"}))
    outcome = gateway.call_tool("demo", "delete_everything")
    assert outcome.status == "denied"
    assert "outside the configured scope" in outcome.reason
    assert calls == []


def test_tool_outside_the_active_run_scope_is_denied():
    gateway = make_gateway(lambda request: httpx.Response(500))
    outcome = gateway.call_tool("demo", "get_weather", allowed_tools=["other_tool"])
    assert outcome.status == "denied"
    assert "active capability scope" in outcome.reason


def test_confirm_risk_requires_approval_and_sends_nothing():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        rid = json.loads(request.content)["id"]
        return json_response({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete"}})

    gateway = make_gateway(handler, declare_risk="confirm")
    outcome = gateway.call_tool("demo", "get_weather")
    assert outcome.status == "approval_required"
    assert calls == []

    approved = gateway.call_tool("demo", "get_weather", explicit_consent=True)
    assert approved.status == "ok"
    assert calls == ["/mcp"]


def test_successful_call_is_audited_and_the_chain_verifies():
    store = MemoryStore(":memory:")
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, require_auth=False, declare_risk="safe")],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
        transport_factory=lambda _config, credential: make_transport(
            echo_result({"resultType": "complete", "content": "sunny"}),
            credential=credential,
        ),
    )
    outcome = gateway.call_tool("demo", "get_weather", {"location": "Cairo"}, session_id="s", run_id="r")
    assert outcome.status == "ok"
    assert outcome.result["content"] == "sunny"

    rows = store.get_audit(run_id="r")
    events = [row["event"] for row in rows]
    assert "mcp.tool_call" in events and "mcp.tool_result" in events
    assert store.verify_audit_chain(run_id="r")["valid"] is True


def test_secrets_in_arguments_never_reach_the_evidence_log():
    store = MemoryStore(":memory:")
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, require_auth=False, declare_risk="safe")],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
        transport_factory=lambda _config, credential: make_transport(
            echo_result({"resultType": "complete"}), credential=credential
        ),
    )
    gateway.call_tool(
        "demo", "get_weather", {"api_key": "sk-live-abcdefghijklmno"}, session_id="s", run_id="r"
    )
    serialized = json.dumps(store.get_audit(run_id="r"), default=str)
    assert "sk-live-abcdefghijklmno" not in serialized
    assert "REDACTED" in serialized


def test_audit_records_the_header_derived_tool_name():
    store = MemoryStore(":memory:")
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, require_auth=False, declare_risk="safe")],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
        transport_factory=lambda _config, credential: make_transport(
            echo_result({"resultType": "complete"}), credential=credential
        ),
    )
    gateway.call_tool("demo", "get_weather", session_id="s", run_id="r")
    payload = store.get_audit(run_id="r")[0]["payload"]
    assert payload["tool"] == namespaced_tool_name("demo", "get_weather")


def test_server_error_becomes_an_error_outcome_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad arguments"}},
            status=200,
        )

    gateway = make_gateway(handler)
    outcome = gateway.call_tool("demo", "get_weather")
    assert outcome.status == "error"
    assert "bad arguments" in outcome.reason


def test_transport_failure_becomes_an_error_outcome():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gateway = make_gateway(handler)
    outcome = gateway.call_tool("demo", "get_weather")
    assert outcome.status == "error"
    assert "did not complete the request" in outcome.reason


def test_input_required_is_surfaced_for_the_caller_to_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "resultType": "input_required",
                    "inputRequests": [{"type": "elicitation", "message": "which region?"}],
                },
            }
        )

    gateway = make_gateway(handler)
    outcome = gateway.call_tool("demo", "get_weather")
    assert outcome.status == "approval_required"
    assert "additional input" in outcome.reason


def test_list_tools_excludes_definitions_with_invalid_headers():
    store = MemoryStore(":memory:")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "resultType": "complete",
            "tools": [
                {"name": "good", "inputSchema": VALID_SCHEMA},
                {"name": "bad", "inputSchema": {"properties": {"a": {"type": "number", "x-mcp-header": "A"}}}},
            ],
        },
    }
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, require_auth=False, declare_risk="safe")],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
        transport_factory=lambda _config, credential: make_transport(
            lambda request: json_response(
                {**payload, "id": json.loads(request.content)["id"]}
            ),
            credential=credential,
        ),
    )
    tools = gateway.list_tools("demo", run_id="r")
    assert [tool.name for tool in tools] == ["good"]
    assert "bad" in gateway.rejected_tools("demo")
    events = [row["event"] for row in store.get_audit(run_id="r")]
    assert "mcp.tool_definition_rejected" in events


def test_capability_scope_is_namespaced():
    gateway = make_gateway(lambda request: httpx.Response(500), allowed_tools=frozenset({"get_weather"}))
    scope = gateway.capability_scope("demo")
    assert scope == {namespaced_tool_name("demo", "get_weather")}
    assert split_namespaced_tool(next(iter(scope))) == ("demo", "get_weather")
    assert split_namespaced_tool("read_csv") is None


def test_describe_contains_no_credential_material(monkeypatch):
    monkeypatch.setenv("DEMO_MCP_TOKEN", "super-secret-token")
    config = MCPServerConfig(
        name="demo",
        url=PUBLIC_ENDPOINT,
        credential_env="DEMO_MCP_TOKEN",
        declare_risk="safe",
    )
    gateway = MCPGateway([config], enabled=True, allow_private_networks=True)
    described = json.dumps(gateway.describe(), default=str)
    assert "super-secret-token" not in described
    # The endpoint itself must also stay clean.
    assert gateway.describe()[0]["endpoint"] == PUBLIC_ENDPOINT


def test_server_requiring_auth_without_a_credential_fails_closed():
    with pytest.raises(MCPAuthError, match="requires authentication"):
        MCPGateway(
            [MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, credential_env="MISSING_MCP_TOKEN")],
            enabled=True,
            allow_private_networks=True,
        )


def test_transport_other_than_streamable_http_is_refused():
    with pytest.raises(MCPGatewayError, match="supported"):
        MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, transport="stdio", require_auth=False)


def test_duplicate_server_names_are_refused():
    config = MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, require_auth=False)
    with pytest.raises(MCPGatewayError, match="duplicate"):
        MCPGateway([config, config], enabled=True, allow_private_networks=True)


def test_reachability_is_rechecked_on_every_call(monkeypatch):
    """A name that resolved publicly at registration must not stay trusted.

    DNS is stubbed so the test stays offline: the first resolution (at
    construction) is public, the second (at call time) is loopback. The call
    must be refused even though registration succeeded.
    """
    import socket

    resolutions = {"n": 0}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        resolutions["n"] += 1
        address = "8.8.8.8" if resolutions["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr("nimna.tools.builtin.web.socket.getaddrinfo", fake_getaddrinfo)

    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=PUBLIC_ENDPOINT, require_auth=False, declare_risk="safe")],
        enabled=True,
        transport_factory=lambda _config, credential: make_transport(
            echo_result({"resultType": "complete"}), credential=credential
        ),
    )
    assert resolutions["n"] == 1  # construction resolved the public address

    outcome = gateway.call_tool("demo", "get_weather")
    assert outcome.status == "denied"
    assert "private address" in outcome.reason
    assert resolutions["n"] == 2  # and the call re-resolved rather than trusting it


def test_public_literal_ip_endpoint_is_accepted_without_dns():
    # A literal public IP needs no resolution, so this asserts the guard does
    # not simply block everything.
    assert_public_url("https://8.8.8.8/mcp")
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url="https://8.8.8.8/mcp", require_auth=False)],
        enabled=True,
    )
    assert gateway.server_names == ("demo",)


# ==========================================================================
# Gateway: environment configuration
# ==========================================================================


def test_servers_from_env_builds_entries_without_reading_the_secret():
    from nimna.mcp import servers_from_env

    configs = servers_from_env(
        {
            "MCP_SERVERS": "demo, files",
            "MCPSERVER_DEMO_URL": "https://mcp.example.com/mcp",
            "MCPSERVER_DEMO_SCOPE": "get_weather, list_files",
            "MCPSERVER_DEMO_TOKEN_ENV": "DEMO_MCP_TOKEN",
            "MCPSERVER_FILES_URL": "https://files.example.com/mcp",
            "MCPSERVER_FILES_AUTH": "false",
        }
    )
    demo, files = configs
    assert demo.name == "demo"
    assert demo.allowed_tools == {"get_weather", "list_files"}
    # The config carries the variable *name*, never the secret.
    assert demo.credential_env == "DEMO_MCP_TOKEN"
    assert demo.require_auth is True
    assert files.require_auth is False
    assert files.credential_env == "MCPSERVER_FILES_TOKEN"


def test_servers_from_env_requires_a_url_for_every_named_server():
    from nimna.mcp import servers_from_env

    with pytest.raises(MCPGatewayError, match="MCPSERVER_DEMO_URL is not set"):
        servers_from_env({"MCP_SERVERS": "demo"})
    with pytest.raises(MCPGatewayError, match="duplicate"):
        servers_from_env({"MCP_SERVERS": "demo,demo", "MCPSERVER_DEMO_URL": "https://a.example.com/mcp"})


def test_servers_from_env_returns_nothing_when_unset():
    from nimna.mcp import servers_from_env

    assert servers_from_env({}) == ()


def test_from_settings_reads_servers_from_env_and_stays_disabled(monkeypatch):
    """Servers may be configured while the gateway is still off."""
    from nimna.config import Settings

    monkeypatch.setenv("MCP_SERVERS", "demo")
    monkeypatch.setenv("MCPSERVER_DEMO_URL", "https://8.8.8.8/mcp")
    monkeypatch.setenv("MCPSERVER_DEMO_AUTH", "false")

    settings = Settings.from_env(env_file=None)
    gateway = MCPGateway.from_settings(settings)
    assert gateway.enabled is False
    assert gateway.server_names == ("demo",)
    # Still refused, because MCP_ENABLED defaults to false.
    assert gateway.call_tool("demo", "anything").status == "denied"


def test_no_request_is_attempted_when_the_gateway_is_disabled(monkeypatch):
    from nimna.config import Settings

    seen: list[str] = []
    monkeypatch.setenv("MCP_SERVERS", "demo")
    # A literal public IP keeps the real reachability check in play with no DNS.
    monkeypatch.setenv("MCPSERVER_DEMO_URL", "https://8.8.8.8/mcp")
    monkeypatch.setenv("MCPSERVER_DEMO_AUTH", "false")

    gateway = MCPGateway.from_settings(
        Settings.from_env(env_file=None),
        transport_factory=lambda _config, credential: make_transport(
            lambda request: seen.append(str(request.url)) or httpx.Response(500),
            credential=credential,
        ),
    )
    gateway.call_tool("demo", "get_weather")
    assert seen == []
