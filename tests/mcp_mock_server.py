"""A minimal MCP server on a real HTTP socket, for offline integration tests.

This exists to close the gap the capability matrix calls
``integration/contract test where external``. A mocked ``httpx`` transport
proves the client does what the test author expected; it cannot catch a client
that sends headers the *server* would have rejected, because the mock never
rejects anything. This server implements the ``2026-07-28`` Streamable HTTP
binding for real — over TCP, on a loopback port — and validates what it
receives, so a wrong client fails.

What it enforces (each maps to a MUST in the revision):

* ``POST`` only; ``GET``/``DELETE`` answer ``405`` (removed in this revision).
* ``Accept`` must list both ``application/json`` and ``text/event-stream``.
* ``Content-Type: application/json``, else ``415``.
* ``MCP-Protocol-Version`` present and equal to the body's ``_meta`` value.
* ``Mcp-Method`` present and equal to the body's ``method``.
* ``Mcp-Name`` present iff the method is name-bearing, and equal to the body.
* ``Mcp-Param-{Name}`` headers agree with the annotated argument, when a tool
  declares ``x-mcp-header``.
* Any header/body disagreement answers ``400`` with ``-32020``.

It deliberately does *not* implement the removed session machinery: an
``Mcp-Session-Id`` header is ignored and never echoed, so a client that depends
on sessions cannot pass against it.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

PROTOCOL_VERSION = "2026-07-28"

CODE_HEADER_MISMATCH = -32020
CODE_UNSUPPORTED_PROTOCOL_VERSION = -32022
CODE_METHOD_NOT_FOUND = -32601
CODE_INVALID_PARAMS = -32602

#: Methods that carry ``params.name`` and therefore require ``Mcp-Name``.
NAME_BEARING = frozenset({"tools/call", "resources/read", "prompts/get"})


@dataclass
class RecordedRequest:
    """One request as the server actually received it."""

    method: str
    body: dict[str, Any]
    headers: dict[str, str]


@dataclass
class MockMCPState:
    """Mutable state shared with the handler thread."""

    server_name: str = "mock-mcp"
    server_version: str = "1.0.0"
    protocol_versions: tuple[str, ...] = (PROTOCOL_VERSION,)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[RecordedRequest] = field(default_factory=list)
    #: Methods answered as an SSE stream instead of a single JSON object.
    sse_methods: set[str] = field(default_factory=set)
    #: Error returned for the next ``tools/call``, as ``{"code":..,"message":..}``.
    fail_tools_call: Optional[dict[str, Any]] = None
    #: Answers every POST with this HTTP status and an empty body.
    blank_status: Optional[int] = None

    def reset(self) -> None:
        self.calls.clear()
        self.fail_tools_call = None
        self.blank_status = None

    @property
    def called_methods(self) -> list[str]:
        return [call.method for call in self.calls]


def _jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: MockMCPState

    def log_message(self, *_args: Any) -> None:  # silence the test output
        pass

    # -- helpers ---------------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, events: list[dict[str, Any]], status: int = 200) -> None:
        chunks = []
        for event in events:
            chunks.append(f"data: {json.dumps(event)}\n\n")
        # A keep-alive comment leads, as the revision recommends for long-lived
        # streams; a client that treats it as malformed data will fail here.
        payload = (": keep-alive\n\n" + "".join(chunks)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _headers_lower(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}

    # -- HTTP verbs ------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        # The GET stream endpoint was removed in this revision.
        self._send_json({"error": "GET is not part of this revision"}, status=405)

    def do_DELETE(self) -> None:  # noqa: N802
        # No sessions exist, so there is nothing to delete.
        self._send_json({"error": "DELETE is not part of this revision"}, status=405)

    def do_POST(self) -> None:  # noqa: N802
        state = self.state
        if state.blank_status is not None:
            self.send_response(state.blank_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(_jsonrpc_error(None, -32700, "Parse error"), status=400)
            return

        headers = self._headers_lower()

        # Content negotiation, as the revision requires of the client.
        if "application/json" not in self.headers.get("Accept", ""):
            self._send_json({"error": "Accept must include application/json"}, status=406)
            return
        if "text/event-stream" not in self.headers.get("Accept", ""):
            self._send_json({"error": "Accept must include text/event-stream"}, status=406)
            return
        if "application/json" not in headers.get("content-type", ""):
            self._send_json({"error": "Content-Type must be application/json"}, status=415)
            return

        request_id = body.get("id")
        method = body.get("method")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}

        # --- header/body agreement (the part a mock transport cannot test) ---
        version_header = headers.get("mcp-protocol-version")
        if not version_header:
            self._send_json(
                _jsonrpc_error(request_id, CODE_HEADER_MISMATCH, "Missing MCP-Protocol-Version"),
                status=400,
            )
            return
        if version_header not in state.protocol_versions:
            self._send_json(
                _jsonrpc_error(
                    request_id,
                    CODE_UNSUPPORTED_PROTOCOL_VERSION,
                    f"unsupported protocol version {version_header!r}",
                    {"requested": version_header, "supported": list(state.protocol_versions)},
                ),
                status=400,
            )
            return
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        if meta.get("io.modelcontextprotocol/protocolVersion") != version_header:
            self._send_json(
                _jsonrpc_error(
                    request_id,
                    CODE_HEADER_MISMATCH,
                    "MCP-Protocol-Version does not match the body",
                ),
                status=400,
            )
            return

        method_header = headers.get("mcp-method")
        if not method_header:
            self._send_json(
                _jsonrpc_error(request_id, CODE_HEADER_MISMATCH, "Missing Mcp-Method"), status=400
            )
            return
        if method_header != method:
            self._send_json(
                _jsonrpc_error(
                    request_id,
                    CODE_HEADER_MISMATCH,
                    f"Mcp-Method header {method_header!r} does not match body {method!r}",
                ),
                status=400,
            )
            return

        body_name = params.get("name") or params.get("uri")
        name_header = headers.get("mcp-name")
        if method in NAME_BEARING:
            if not body_name:
                self._send_json(
                    _jsonrpc_error(request_id, CODE_INVALID_PARAMS, "missing params.name"),
                    status=400,
                )
                return
            if name_header is None:
                self._send_json(
                    _jsonrpc_error(request_id, CODE_HEADER_MISMATCH, "Missing Mcp-Name"), status=400
                )
                return
            if name_header != body_name:
                self._send_json(
                    _jsonrpc_error(
                        request_id,
                        CODE_HEADER_MISMATCH,
                        f"Mcp-Name {name_header!r} does not match body {body_name!r}",
                    ),
                    status=400,
                )
                return
        elif name_header is not None:
            self._send_json(
                _jsonrpc_error(
                    request_id, CODE_HEADER_MISMATCH, "Mcp-Name present but the body carries no name"
                ),
                status=400,
            )
            return

        if method == "tools/call":
            definition = state.tools.get(str(body_name))
            if definition is None:
                self._send_json(
                    _jsonrpc_error(request_id, CODE_INVALID_PARAMS, f"unknown tool {body_name!r}"),
                    status=200,
                )
                return
            mismatch = self._check_param_headers(headers, definition, params.get("arguments") or {})
            if mismatch:
                self._send_json(
                    _jsonrpc_error(request_id, CODE_HEADER_MISMATCH, mismatch), status=400
                )
                return

        state.calls.append(RecordedRequest(method=str(method), body=body, headers=headers))

        # --- dispatch --------------------------------------------------
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "protocolVersions": list(state.protocol_versions),
                "serverInfo": {"name": state.server_name, "version": state.server_version},
                "capabilities": {"tools": {}},
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": state.server_name,
                        "version": state.server_version,
                    }
                },
            }
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": [definition for definition in state.tools.values()],
                "ttlMs": 60000,
                "cacheScope": "private",
            }
        elif method == "tools/call":
            if state.fail_tools_call is not None:
                self._send_json(
                    _jsonrpc_error(
                        request_id,
                        int(state.fail_tools_call["code"]),
                        str(state.fail_tools_call["message"]),
                    ),
                    status=200,
                )
                return
            definition = state.tools[str(body_name)]
            arguments = params.get("arguments") or {}
            result = {
                "resultType": "complete",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"tool": body_name, "echo": arguments, "served_by": state.server_name}
                        ),
                    }
                ],
                "structuredContent": {"echo": arguments},
            }
        else:
            self._send_json(
                _jsonrpc_error(request_id, CODE_METHOD_NOT_FOUND, f"Method not found: {method}"),
                status=404,
            )
            return

        response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        if method in state.sse_methods:
            self._send_sse(
                [
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progress": 1, "total": 2},
                    },
                    response,
                ]
            )
        else:
            self._send_json(response)

    @staticmethod
    def _check_param_headers(
        headers: dict[str, str], definition: dict[str, Any], arguments: dict[str, Any]
    ) -> Optional[str]:
        """Validate ``Mcp-Param-*`` headers against the annotated arguments."""
        schema = definition.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        for name, subschema in properties.items():
            if not isinstance(subschema, dict):
                continue
            annotation = subschema.get("x-mcp-header")
            if not annotation:
                continue
            header_name = f"mcp-param-{annotation}".lower()
            present = name in arguments and arguments[name] is not None
            received = headers.get(header_name)
            if present and received is None:
                return f"missing required header Mcp-Param-{annotation}"
            if present:
                value = arguments[name]
                if isinstance(value, bool):
                    value = "true" if value else "false"
                if str(received) != str(value):
                    return (
                        f"Mcp-Param-{annotation} header {received!r} does not match "
                        f"body value {value!r}"
                    )
            if not present and received is not None:
                return f"Mcp-Param-{annotation} present but the argument is absent"
        return None


class MockMCPServer:
    """A running MCP server on loopback. Use as a context manager."""

    def __init__(self, *, tools: Optional[dict[str, dict[str, Any]]] = None, **state_kwargs: Any):
        self.state = MockMCPState(tools=dict(tools or {}), **state_kwargs)
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def __enter__(self) -> "MockMCPServer":
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def stop(self) -> None:
        self.__exit__()


def default_tools() -> dict[str, dict[str, Any]]:
    """A small tool set, including one with an ``x-mcp-header`` annotation."""
    return {
        "get_weather": {
            "name": "get_weather",
            "description": "Look up the weather for a location.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name"},
                    "days": {"type": "integer", "description": "Forecast length", "default": 1},
                },
                "required": ["location"],
            },
        },
        "execute_sql": {
            "name": "execute_sql",
            "description": "Execute a query in a named region.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "x-mcp-header": "Region"},
                    "query": {"type": "string"},
                },
                "required": ["region", "query"],
            },
        },
    }


__all__ = [
    "CODE_HEADER_MISMATCH",
    "MockMCPServer",
    "MockMCPState",
    "PROTOCOL_VERSION",
    "RecordedRequest",
    "default_tools",
]
