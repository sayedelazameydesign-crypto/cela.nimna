"""Streamable HTTP transport for MCP.

One endpoint, one POST per JSON-RPC message, and a reply that is either a
single JSON object or an SSE stream scoped to that request.

Three properties of the ``2026-07-28`` binding shape this implementation, and
each of them is a place where copying an older tutorial produces something that
looks correct and is not:

* **No sessions.** There is no ``Mcp-Session-Id`` to mint, echo, or delete, and
  no ``initialize`` handshake. Sending either is not a harmless extra.
* **No resumability.** ``Last-Event-ID`` and SSE event ids are gone. If a
  response stream breaks, the in-flight request is lost and must be re-issued
  with a *new* request id. This transport therefore never retries internally:
  retrying under the caller's back is indistinguishable from the request having
  succeeded twice.
* **Cancellation is the disconnect.** Closing the response stream cancels the
  request; there is no ``notifications/cancelled`` on this transport.

The client is injectable for the same reason ``BrowserUseV4Client`` is: the
contract must be provable offline, without reaching a real MCP server.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Optional

import httpx

from .auth import MCPCredential
from .contract import (
    CODE_METHOD_NOT_FOUND,
    MCPProtocolError,
    MCPRequest,
    MCPResponse,
    PROTOCOL_VERSION,
    parse_response,
)
from .headers import (
    ACCEPT_VALUE,
    CONTENT_TYPE_JSON,
    check_header_body_agreement,
    derive_headers,
)

#: Cap on a single response body, SSE included. An MCP server that streams
#: without bound must not be able to exhaust the gateway's memory.
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class MCPTransportError(RuntimeError):
    """The transport could not deliver or read a message."""


class MCPTimeoutError(MCPTransportError):
    """The request timed out. The server may still be working."""


class MCPStreamTruncatedError(MCPTransportError):
    """The response stream ended without a final response.

    Not retried here on purpose: without resumability the request is lost, and
    re-issuing it is a decision the caller has to make with a new id.
    """


class MCPLegacyServerError(MCPTransportError):
    """The endpoint answered with a ``400`` this revision does not define.

    Per the specification's backward-compatibility rule, that is the signal
    that the endpoint speaks an ``initialize``-based revision. This transport
    does not implement those revisions, so it reports the condition instead of
    guessing — a gateway that silently fell back would be sending a handshake
    it cannot validate.
    """


class MCPResponseTooLargeError(MCPTransportError):
    """The response exceeded the configured size cap."""


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SSEEvent:
    """One dispatched Server-Sent Events message."""

    data: str
    event: Optional[str] = None
    #: Present when the server sends one. Recorded, never used to resume:
    #: ``Last-Event-ID`` redelivery was removed in this revision.
    id: Optional[str] = None


def iter_sse_events(lines: Iterator[str]) -> Iterator[SSEEvent]:
    """Group raw SSE lines into dispatched events.

    Comment lines (leading ``:``) carry no data and are ignored — the revision
    recommends them as keep-alives on long-lived streams, and treating one as
    malformed input is a common way to break an idle ``subscriptions/listen``.
    """
    data_lines: list[str] = []
    event_name: Optional[str] = None
    event_id: Optional[str] = None

    def dispatch() -> Optional[SSEEvent]:
        nonlocal data_lines, event_name, event_id
        if not data_lines and event_name is None:
            return None
        event = SSEEvent(data="\n".join(data_lines), event=event_name, id=event_id)
        data_lines = []
        event_name = None
        event_id = None
        return event

    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            event = dispatch()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        # `retry` and unknown fields are ignored rather than treated as errors.
    event = dispatch()
    if event is not None:
        yield event


def parse_sse_body(text: str) -> tuple[SSEEvent, ...]:
    """Parse a complete SSE body. Convenience for tests and buffered reads."""
    return tuple(iter_sse_events(iter(text.splitlines())))


# --------------------------------------------------------------------------
# Transport interface
# --------------------------------------------------------------------------


class MCPTransport(ABC):
    """What the gateway needs from any MCP binding."""

    @property
    @abstractmethod
    def endpoint(self) -> str:
        """A stable identifier for the target, safe to log."""

    @abstractmethod
    def send(
        self,
        request: MCPRequest,
        *,
        tool_definition: Optional[Mapping[str, Any]] = None,
        on_notification: Optional[Callable[[SSEEvent], None]] = None,
    ) -> MCPResponse:
        """Send one request and return its response."""

    def close(self) -> None:  # pragma: no cover - default is a no-op
        """Release transport resources."""

    def __enter__(self) -> "MCPTransport":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class StreamableHTTPTransport(MCPTransport):
    """Streamable HTTP binding, protocol revision ``2026-07-28``."""

    def __init__(
        self,
        url: str,
        *,
        credential: Optional[MCPCredential] = None,
        protocol_version: str = PROTOCOL_VERSION,
        timeout: float = 60.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Optional[httpx.BaseTransport] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        if not url or not url.strip():
            raise ValueError("MCP endpoint URL is required")
        self._url = url.strip()
        self.credential = credential
        self.protocol_version = protocol_version
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._owns_client = http_client is None
        if http_client is not None:
            self.client = http_client
        else:
            self.client = httpx.Client(timeout=timeout, transport=transport)

    @property
    def endpoint(self) -> str:
        """The endpoint URL with any userinfo stripped.

        A URL is the most likely place for a credential to be pasted by
        accident (``https://user:token@host/mcp``), so credentials embedded in
        userinfo are dropped before the URL is ever logged.
        """
        parsed = httpx.URL(self._url)
        if parsed.userinfo:
            parsed = parsed.copy_with(userinfo=b"")
        return str(parsed)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    # -- request ---------------------------------------------------------

    def _build_headers(
        self, request: MCPRequest, tool_definition: Optional[Mapping[str, Any]]
    ) -> dict[str, str]:
        headers = derive_headers(
            request, protocol_version=self.protocol_version, tool_definition=tool_definition
        )
        # Refuse to send a request whose headers disagree with its body. The
        # server would reject it as a HeaderMismatch anyway; catching it here
        # keeps the gateway from being the component that introduced the split
        # between the value a router reads and the value the server executes.
        check_header_body_agreement(headers, request.to_wire())
        if self.credential is not None:
            headers = self.credential.apply(headers)
        return headers

    def send(
        self,
        request: MCPRequest,
        *,
        tool_definition: Optional[Mapping[str, Any]] = None,
        on_notification: Optional[Callable[[SSEEvent], None]] = None,
    ) -> MCPResponse:
        body = request.to_wire()
        headers = self._build_headers(request, tool_definition)
        try:
            with self.client.stream(
                "POST", self._url, json=body, headers=headers, timeout=self.timeout
            ) as response:
                return self._read_response(response, request, on_notification=on_notification)
        except httpx.TimeoutException as exc:
            raise MCPTimeoutError(
                f"MCP request to {self.endpoint} timed out; the server may still be working"
            ) from exc
        except httpx.HTTPError as exc:
            raise MCPTransportError(f"MCP transport error against {self.endpoint}: {exc}") from exc

    def _read_response(
        self,
        response: httpx.Response,
        request: MCPRequest,
        *,
        on_notification: Optional[Callable[[SSEEvent], None]] = None,
    ) -> MCPResponse:
        status = response.status_code
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()

        if status == 202:
            # Legal only for a notification, and a request is never a
            # notification: accepting this would silently discard the caller's
            # request and report an empty result.
            raise MCPProtocolError(
                "server answered a request with 202 Accepted, which is only valid for notifications"
            )

        if status >= 400:
            return self._read_error_response(response, request, status, content_type)

        if content_type == "text/event-stream":
            return self._read_stream(response, request, on_notification=on_notification)
        if content_type == CONTENT_TYPE_JSON or not content_type:
            payload = self._read_bounded_text(response)
            if not payload.strip():
                raise MCPTransportError(
                    f"server returned {status} with an empty body for a request"
                )
            return parse_response(payload, request_id=request.id)

        raise MCPTransportError(
            f"unsupported response content-type {content_type!r} for a request"
        )

    def _read_error_response(
        self,
        response: httpx.Response,
        request: MCPRequest,
        status: int,
        content_type: str,
    ) -> MCPResponse:
        raw = self._read_bounded_text(response)
        if raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if isinstance(payload, Mapping) and payload.get("jsonrpc") == "2.0" and "error" in payload:
                # A recognised JSON-RPC error means the server speaks a modern
                # revision, including when the code is UnsupportedProtocolVersion
                # or HeaderMismatch. Falling back to `initialize` on these would
                # be the wrong move, so they are returned as responses and the
                # gateway reports them as an error outcome.
                return parse_response(payload, request_id=request.id)

        if status == 400:
            # `400` without a recognised modern error is the documented signal
            # for an initialize-based server. This contract does not implement
            # those revisions, so report it rather than guess at a handshake.
            raise MCPLegacyServerError(
                f"{self.endpoint} answered 400 without a recognised MCP error body; "
                "this contract targets the stateless 2026-07-28 revision, and "
                "initialize-based revisions are not implemented"
            )
        if status == 404:
            # A 404 carrying a JSON-RPC error was returned above. A bare 404 is
            # the distinct case of a URL that does not host a modern MCP
            # endpoint at all — typically a legacy HTTP+SSE server or a typo.
            raise MCPLegacyServerError(
                f"{self.endpoint} answered 404 without a JSON-RPC error body; "
                "the URL may point at a legacy HTTP+SSE endpoint"
            )
        if status == 405:
            raise MCPProtocolError(
                "server answered 405 Method Not Allowed; MCP endpoints accept POST",
                code=CODE_METHOD_NOT_FOUND,
            )
        if status == 406:
            raise MCPProtocolError(
                "server rejected the Accept header; MCP requires "
                f"{ACCEPT_VALUE!r}"
            )
        if status == 415:
            raise MCPProtocolError("server rejected Content-Type; MCP requires application/json")

        raise MCPTransportError(
            f"MCP request to {self.endpoint} failed with HTTP {status}"
        )

    def _read_bounded_text(self, response: httpx.Response) -> str:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self.max_response_bytes:
                raise MCPResponseTooLargeError(
                    f"response exceeded {self.max_response_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _read_stream(
        self,
        response: httpx.Response,
        request: MCPRequest,
        *,
        on_notification: Optional[Callable[[SSEEvent], None]] = None,
    ) -> MCPResponse:
        """Read a request-scoped SSE stream up to its final response."""
        total = 0

        def bounded_lines() -> Iterator[str]:
            nonlocal total
            for line in response.iter_lines():
                total += len(line.encode("utf-8")) + 1
                if total > self.max_response_bytes:
                    raise MCPResponseTooLargeError(
                        f"response stream exceeded {self.max_response_bytes} bytes"
                    )
                yield line

        deferring = response.headers.get("X-Accel-Buffering", "").strip().lower() == "no"
        if deferring:  # pragma: no cover - information only
            # Kept as an observable note rather than behaviour: the header tells
            # us a proxy will not buffer, which is what makes progress
            # notifications arrive on time. Nothing to do about its absence.
            pass

        for event in iter_sse_events(bounded_lines()):
            if not event.data:
                continue
            try:
                payload = json.loads(event.data)
            except ValueError:
                raise MCPProtocolError("SSE event data is not valid JSON") from None
            if not isinstance(payload, Mapping):
                raise MCPProtocolError("SSE event data must be a JSON object")

            if "id" not in payload and "method" in payload:
                # A request-scoped notification. The revision guarantees it
                # relates to this request; it must not be answered.
                if on_notification is not None:
                    on_notification(event)
                continue

            parsed = parse_response(payload, request_id=request.id)
            return parsed

        raise MCPStreamTruncatedError(
            f"stream from {self.endpoint} ended without a final response for request "
            f"{request.id!r}; re-issue with a new id (streams are not resumable)"
        )


@contextmanager
def open_transport(transport: MCPTransport) -> Iterator[MCPTransport]:
    """Close a transport even when the caller raises."""
    try:
        yield transport
    finally:
        transport.close()


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "MCPLegacyServerError",
    "MCPResponseTooLargeError",
    "MCPStreamTruncatedError",
    "MCPTimeoutError",
    "MCPTransport",
    "MCPTransportError",
    "SSEEvent",
    "StreamableHTTPTransport",
    "iter_sse_events",
    "open_transport",
    "parse_sse_body",
]
