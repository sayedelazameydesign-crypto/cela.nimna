"""Mirrored request-metadata headers — the header/body agreement boundary.

The Streamable HTTP transport mirrors selected JSON-RPC body fields into HTTP
headers so that intermediaries can route and inspect a request without parsing
the body. That mirroring is only safe if something prevents the two sources of
truth from disagreeing, because a load balancer may route on the header while
the server executes the body.

This module owns three separate jobs, all of them offline and deterministic:

1. **Encoding.** Turn a JSON value into a legal HTTP field value, using the
   ``=?base64?...?=`` sentinel when a plain value would be unsafe or ambiguous.
2. **Tool-definition validation.** Reject ``x-mcp-header`` annotations that do
   not satisfy the revision's constraints, which requires excluding the whole
   tool from ``tools/list`` rather than emitting a header nobody can validate.
3. **Agreement checking.** Refuse to route a request whose headers do not agree
   with its body, and refuse to route on header values from a revision that
   never agreed headers with the body in the first place.

Job 3 is the reason this module exists as a named boundary rather than a few
lines inside the transport.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .contract import (
    CODE_HEADER_MISMATCH,
    MCPProtocolError,
    MCPRequest,
    METHOD_TOOLS_CALL,
    PROTOCOL_VERSION,
    name_for_request,
    version_carries_headers,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Header names
# --------------------------------------------------------------------------

HEADER_PROTOCOL_VERSION = "MCP-Protocol-Version"
HEADER_METHOD = "Mcp-Method"
HEADER_NAME = "Mcp-Name"
PARAM_HEADER_PREFIX = "Mcp-Param-"

#: Schema extension that marks a tool parameter for header mirroring.
X_MCP_HEADER = "x-mcp-header"

ACCEPT_VALUE = "application/json, text/event-stream"
CONTENT_TYPE_JSON = "application/json"

# --------------------------------------------------------------------------
# Value encoding
# --------------------------------------------------------------------------

_BASE64_PREFIX = "=?base64?"
_BASE64_SUFFIX = "?="

# RFC 9110 field values: visible ASCII, space, and horizontal tab.
_HEADER_VALUE_CHARS = frozenset(chr(code) for code in range(0x21, 0x7F)) | {"\t", " "}

# RFC 9110 field-name token syntax (1*tchar).
_TCHAR_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class HeaderContractError(MCPProtocolError):
    """A header could not be built, or does not agree with the request body."""

    def __init__(self, message: str, *, data: Any = None):
        super().__init__(message, code=CODE_HEADER_MISMATCH, data=data)


def is_plain_header_value(value: str) -> bool:
    """True when ``value`` survives a plain-text round trip.

    Rejects non-ASCII, control characters, and leading/trailing whitespace.
    Empty is treated as unsafe so that "value is empty" stays distinguishable
    from "header omitted" after any intermediary trims or drops it.
    """
    if not value:
        return False
    if not set(value) <= _HEADER_VALUE_CHARS:
        return False
    if value != value.strip(" \t"):
        return False
    # A plain value that happens to look like the sentinel would be decoded by
    # a conforming reader, so it must be encoded like any other unsafe value.
    if value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX):
        return False
    return True


def encode_header_value(value: str) -> str:
    """Encode a string for use as an HTTP field value."""
    if is_plain_header_value(value):
        return value
    payload = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"{_BASE64_PREFIX}{payload}{_BASE64_SUFFIX}"


def decode_header_value(value: str) -> str:
    """Reverse :func:`encode_header_value`.

    Raises rather than guessing: a value carrying the sentinel must be valid
    Base64 of valid UTF-8, because a reader that silently accepted garbage
    here would compare a nonsense value against the body and could be walked
    into believing the two agree.
    """
    if not (value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX)):
        return value
    payload = value[len(_BASE64_PREFIX) : len(value) - len(_BASE64_SUFFIX)] if value.endswith(_BASE64_SUFFIX) else ""
    try:
        return base64.b64decode(payload.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HeaderContractError(f"malformed base64 header value: {exc}") from exc


def _value_to_text(value: Any) -> str:
    """Convert a tool argument to its string representation for mirroring."""
    if isinstance(value, bool):
        # bool is an int subclass in Python; check it first.
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        raise HeaderContractError(
            "number parameters are not permitted for x-mcp-header; use integer or string"
        )
    raise HeaderContractError(f"unsupported value type for header mirroring: {type(value).__name__}")


#: JavaScript-safe integer range, which the revision requires for mirrored ints.
_JS_SAFE_INT_MAX = 2**53 - 1


def _check_integer_range(value: int) -> None:
    if abs(value) > _JS_SAFE_INT_MAX:
        raise HeaderContractError(
            f"integer {value} exceeds the JavaScript safe integer range for header mirroring"
        )


# --------------------------------------------------------------------------
# x-mcp-header tool-definition validation
# --------------------------------------------------------------------------

_PRIMITIVE_TYPES = frozenset({"string", "integer", "boolean"})


@dataclass(frozen=True)
class HeaderParam:
    """A tool parameter designated for header mirroring."""

    #: The ``{Name}`` portion; the header is ``Mcp-Param-{name}``.
    name: str
    #: Property path from the schema root, e.g. ``("region",)``.
    path: tuple[str, ...]
    #: Declared JSON Schema type of the parameter.
    type: str

    @property
    def header_name(self) -> str:
        return f"{PARAM_HEADER_PREFIX}{self.name}"


class InvalidToolDefinitionError(HeaderContractError):
    """An ``x-mcp-header`` annotation violates the revision's constraints.

    The constraints are mandatory: a client on this revision must reject the
    tool definition, not merely ignore the annotation.
    """


def header_params_from_schema(schema: Any) -> tuple[HeaderParam, ...]:
    """Collect and validate every ``x-mcp-header`` annotation in a schema.

    Raises :class:`InvalidToolDefinitionError` for any violation, including a
    duplicate header name and an annotation in a position that is not
    statically reachable, because the revision requires the caller to exclude
    the tool entirely rather than emit a partially-validated header set.

    Detection walks the whole schema, not just ``properties``: an annotation
    buried under ``items`` or ``oneOf`` is a violation of the contract, and
    walking only the reachable paths would let it pass unnoticed.
    """
    if not isinstance(schema, Mapping):
        return ()

    found: list[HeaderParam] = []

    # Keywords whose contents are not schemas and cannot hold an annotation.
    _OPAQUE_KEYS = frozenset(
        {"type", "description", "title", "required", "enum", "const", "default",
         "examples", "format", "x-mcp-header", "$ref"}
    )

    def walk(node: Mapping[str, Any], path: tuple[str, ...], reachable: bool) -> None:
        annotation = node.get(X_MCP_HEADER)
        if annotation is not None:
            if not reachable:
                # The revision makes this fatal: the annotation is invalid, and
                # so is the tool definition carrying it.
                raise InvalidToolDefinitionError(
                    f"x-mcp-header on {'.'.join(path) or '<root>'} is not statically reachable "
                    "through properties keys only"
                )
            found.append(_validate_annotation(annotation, path, node))

        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for key, child in properties.items():
                if isinstance(child, Mapping):
                    walk(child, path + (str(key),), reachable)

        for key, child in node.items():
            if key == "properties" or key in _OPAQUE_KEYS:
                continue
            if isinstance(child, Mapping):
                walk(child, path + (str(key),), False)
            elif isinstance(child, (list, tuple)):
                for index, item in enumerate(child):
                    if isinstance(item, Mapping):
                        walk(item, path + (str(key), str(index)), False)

    walk(schema, (), True)

    seen: dict[str, str] = {}
    for param in found:
        lowered = param.name.lower()
        if lowered in seen:
            raise InvalidToolDefinitionError(
                f"duplicate x-mcp-header value {param.name!r} (case-insensitive)"
            )
        seen[lowered] = param.name
    return tuple(found)


def _validate_annotation(annotation: Any, path: tuple[str, ...], subschema: Mapping[str, Any]) -> HeaderParam:
    where = ".".join(path)
    if not isinstance(annotation, str):
        raise InvalidToolDefinitionError(f"x-mcp-header on {where} must be a string")
    if not annotation:
        raise InvalidToolDefinitionError(f"x-mcp-header on {where} must not be empty")
    if not _TCHAR_RE.match(annotation):
        raise InvalidToolDefinitionError(
            f"x-mcp-header value {annotation!r} on {where} is not a valid HTTP field name token"
        )
    declared = subschema.get("type")
    if declared not in _PRIMITIVE_TYPES:
        raise InvalidToolDefinitionError(
            f"x-mcp-header on {where} requires primitive type "
            f"({'/'.join(sorted(_PRIMITIVE_TYPES))}), got {declared!r}"
        )
    return HeaderParam(name=annotation, path=path, type=str(declared))


def filter_tool_definitions(
    definitions: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    """Drop tool definitions whose headers violate the contract.

    Returns ``(accepted, rejected)`` where ``rejected`` maps a tool name to the
    reason. One malformed definition must not remove the other valid tools, so
    rejection is per-tool and the reason is logged and returned for audit.
    """
    accepted: dict[str, Mapping[str, Any]] = {}
    rejected: dict[str, str] = {}
    for name, definition in definitions.items():
        try:
            header_params_from_schema(definition.get("inputSchema"))
        except InvalidToolDefinitionError as exc:
            rejected[str(name)] = str(exc)
            log.warning("rejecting tool %r: %s", name, exc)
            continue
        accepted[str(name)] = definition
    return accepted, rejected


def extract_param_headers(
    schema_or_definition: Mapping[str, Any], arguments: Mapping[str, Any]
) -> dict[str, str]:
    """Build ``Mcp-Param-*`` headers for one ``tools/call``.

    Accepts either a tool definition (``{"name": ..., "inputSchema": ...}``) or
    a bare JSON Schema, since callers reasonably hold one or the other.

    Absent values omit the header; a present ``null`` also omits it, because
    the revision distinguishes "provided" from "not provided" by presence in
    the arguments object, and a header carrying ``null`` would be undecodable.
    """
    schema = (
        schema_or_definition["inputSchema"]
        if "inputSchema" in schema_or_definition
        else schema_or_definition
    )
    headers: dict[str, str] = {}
    for param in header_params_from_schema(schema):
        node: Any = arguments
        present = True
        for key in param.path:
            if not isinstance(node, Mapping) or key not in node:
                present = False
                break
            node = node[key]
        if not present or node is None:
            continue
        if param.type == "integer":
            if isinstance(node, bool) or not isinstance(node, int):
                raise HeaderContractError(
                    f"parameter {'.'.join(param.path)} is declared integer but got {type(node).__name__}"
                )
            _check_integer_range(node)
        elif param.type == "boolean":
            if not isinstance(node, bool):
                raise HeaderContractError(
                    f"parameter {'.'.join(param.path)} is declared boolean but got {type(node).__name__}"
                )
        elif param.type == "string" and not isinstance(node, str):
            raise HeaderContractError(
                f"parameter {'.'.join(param.path)} is declared string but got {type(node).__name__}"
            )
        headers[param.header_name] = encode_header_value(_value_to_text(node))
    return headers


# --------------------------------------------------------------------------
# Outbound header derivation
# --------------------------------------------------------------------------


def derive_headers(
    request: MCPRequest,
    *,
    protocol_version: str = PROTOCOL_VERSION,
    tool_definition: Optional[Mapping[str, Any]] = None,
) -> dict[str, str]:
    """Build the required headers for one request.

    Every POST must carry ``MCP-Protocol-Version`` and ``Mcp-Method``;
    name-bearing methods additionally carry ``Mcp-Name``; a ``tools/call``
    whose definition has ``x-mcp-header`` annotations carries ``Mcp-Param-*``.
    """
    if not version_carries_headers(protocol_version):
        raise HeaderContractError(
            f"protocol version {protocol_version!r} does not define mirrored request headers; "
            "headers must not be used for policy decisions on this revision"
        )
    if protocol_version != request.params.get("_meta", {}).get(
        "io.modelcontextprotocol/protocolVersion"
    ):
        # The header and the body must agree; build_meta already wrote the body
        # value, so a mismatch here means the caller passed conflicting inputs.
        raise HeaderContractError(
            "protocol_version disagrees with the value already present in the request _meta"
        )

    headers: dict[str, str] = {
        HEADER_PROTOCOL_VERSION: protocol_version,
        HEADER_METHOD: request.method,
        "Accept": ACCEPT_VALUE,
        "Content-Type": CONTENT_TYPE_JSON,
    }

    name = name_for_request(request)
    if request.method in {METHOD_TOOLS_CALL} and name is None:
        raise HeaderContractError(f"{request.method} requires a non-empty params.name")
    if name is not None:
        headers[HEADER_NAME] = encode_header_value(name)

    if tool_definition is not None:
        arguments = request.params.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = {}
        headers.update(extract_param_headers(tool_definition, arguments))
    return headers


# --------------------------------------------------------------------------
# Agreement checking
# --------------------------------------------------------------------------


def check_header_body_agreement(headers: Mapping[str, str], body: Mapping[str, Any]) -> None:
    """Verify mirrored headers agree with the request body.

    Called before a request leaves the gateway. Sending a request whose headers
    disagree with its body is how a gateway becomes the confused deputy in the
    middle of a routing decision, so disagreement is refused locally rather
    than left to the server to notice.
    """
    lowered = {str(key).lower(): value for key, value in headers.items()}

    method_header = lowered.get(HEADER_METHOD.lower())
    if method_header is None:
        raise HeaderContractError(f"missing required header {HEADER_METHOD}")
    if method_header != body.get("method"):
        raise HeaderContractError(
            f"Header mismatch: {HEADER_METHOD} header value {method_header!r} "
            f"does not match body value {body.get('method')!r}"
        )

    params = body.get("params")
    params = params if isinstance(params, Mapping) else {}
    body_version = (params.get("_meta") or {}).get("io.modelcontextprotocol/protocolVersion") \
        if isinstance(params.get("_meta"), Mapping) else None
    version_header = lowered.get(HEADER_PROTOCOL_VERSION.lower())
    if not version_header:
        raise HeaderContractError(f"missing required header {HEADER_PROTOCOL_VERSION}")
    if version_header != body_version:
        raise HeaderContractError(
            f"Header mismatch: {HEADER_PROTOCOL_VERSION} header value {version_header!r} "
            f"does not match body value {body_version!r}"
        )

    expected_name = None
    for key in ("name", "uri"):
        value = params.get(key)
        if isinstance(value, str) and value:
            expected_name = value
            break
    name_header = lowered.get(HEADER_NAME.lower())
    if expected_name is not None:
        if name_header is None:
            raise HeaderContractError(
                f"Header mismatch: {HEADER_NAME} header is required for method {body.get('method')!r}"
            )
        if decode_header_value(name_header) != expected_name:
            raise HeaderContractError(
                f"Header mismatch: {HEADER_NAME} header value does not match body value {expected_name!r}"
            )
    elif name_header is not None:
        raise HeaderContractError(
            f"header {HEADER_NAME} is present but the body carries no name"
        )


def assert_routable_version(headers: Mapping[str, str]) -> str:
    """Return the version a routing policy may trust, or raise.

    Mirrors the specification's rule for intermediaries: policy that is
    enforced on mirrored headers must confirm the request is from a revision
    that requires header/body validation. On an older or absent version the
    headers were never checked against the body, so they must not be trusted.
    """
    lowered = {str(key).lower(): value for key, value in headers.items()}
    version = lowered.get(HEADER_PROTOCOL_VERSION.lower())
    if not version:
        raise HeaderContractError(
            f"refusing to apply header-based policy without {HEADER_PROTOCOL_VERSION}"
        )
    if not version_carries_headers(version):
        raise HeaderContractError(
            f"refusing to apply header-based policy to protocol version {version!r}, "
            "which does not require header/body validation"
        )
    return version


__all__ = [
    "ACCEPT_VALUE",
    "CONTENT_TYPE_JSON",
    "HEADER_METHOD",
    "HEADER_NAME",
    "HEADER_PROTOCOL_VERSION",
    "HeaderContractError",
    "HeaderParam",
    "InvalidToolDefinitionError",
    "PARAM_HEADER_PREFIX",
    "X_MCP_HEADER",
    "assert_routable_version",
    "check_header_body_agreement",
    "decode_header_value",
    "derive_headers",
    "encode_header_value",
    "extract_param_headers",
    "filter_tool_definitions",
    "header_params_from_schema",
    "is_plain_header_value",
]
