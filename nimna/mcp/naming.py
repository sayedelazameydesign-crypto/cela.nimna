"""Naming rules for governed MCP tools.

Three names are involved in a remote tool call, and conflating them is how a
call silently goes to the wrong place:

1. **The remote name** — ``params.name`` on the wire, exactly as the MCP server
   advertises it. This revision places no character constraint on it beyond
   being a string, and an Arabic-language server may well expose ``طقس``.
2. **The local name** — what the agent's registry, the capability scope and the
   model's function-calling API see. Those consumers are stricter than MCP:
   function names are typically ``[A-Za-z0-9_-]{1,64}``. A raw Arabic name
   would be advertised to the model and rejected there, at the point where the
   failure is least diagnosable.
3. **The header value** — ``Mcp-Name``. Handled in :mod:`nimna.mcp.headers`,
   which Base64-encodes anything that is not a plain ASCII field value.

This module maps (1) → (2) deterministically: ASCII alphanumerics plus ``_``,
``-`` and ``.`` survive unchanged, and everything else is escaped as ``~`` plus
its code point in four fixed-width base36 digits. Fixed width matters — it keeps
the escape self-delimiting without a terminator, and base36 keeps it short
enough that a normal Arabic name fits unhashed (``طقس`` becomes ``~00c7~00d3``,
five characters per letter, covering the whole Unicode range in exactly four
digits). A name that still exceeds the local budget is truncated and suffixed
with a hash of the full remote name, so two long names cannot collapse onto one
local name.

The mapping is deliberately *not* used to reconstruct the remote name for a
call: :func:`nimna.mcp.registry.build_tool` closes over the real remote name, so
a decode bug cannot redirect a call. Decoding exists only so diagnostics and
scopes can be read by a human.

The server name is different: it is operator configuration, not remote input, so
it stays a short ASCII identifier and is validated rather than escaped. Bounding
its length is what keeps the local budget reachable for the escaped element.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

NAMESPACE_PREFIX = "mcp"
NAMESPACE_SEPARATOR = "__"

#: Server names appear inside every namespaced tool name, so the budget for the
#: element depends on them. Bounded here rather than letting a 63-character
#: server name consume the whole name and push the escaping into a hash.
MAX_SERVER_NAME = 24

#: Local tool-name budget. Chosen to sit inside the tightest common
#: function-name limit (64 characters) rather than at the edge of the loosest.
MAX_LOCAL_TOOL_NAME = 64

#: Characters that are safe to keep literally in a local name element.
_PLAIN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)

#: Glob metacharacters, preserved when encoding a pattern but escaped in a name.
_GLOB_CHARS = frozenset("*?[]")

SERVER_NAME_RE = re.compile(rf"^[a-z0-9][a-z0-9_\-]{{0,{MAX_SERVER_NAME - 1}}}$")

_ESCAPE = "~"


class MCPNameError(ValueError):
    """A name cannot be expressed safely."""


def validate_server_name(name: str) -> str:
    """Validate an operator-supplied server name.

    ASCII, short, lowercase: it is configuration, it appears in every local
    tool name, and escaping it would make scopes unreadable.
    """
    if not SERVER_NAME_RE.match(str(name or "")):
        raise MCPNameError(
            f"server name {name!r} must match {SERVER_NAME_RE.pattern!r} "
            f"(max {MAX_SERVER_NAME} characters)"
        )
    return str(name)


#: Base36 alphabet: short escapes that still cover all of Unicode in four digits
#: (36**4 = 1_679_616 > 0x10FFFF), so an escape is always five characters.
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_ESCAPE_WIDTH = 4


def _to_base36(value: int) -> str:
    if value <= 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(_B36[remainder])
    return "".join(reversed(digits))


def _escape(ch: str) -> str:
    return _ESCAPE + _to_base36(ord(ch)).rjust(_ESCAPE_WIDTH, "0")


def encode_tool_name_element(remote_name: str) -> str:
    """Escape a remote tool name into a local name element.

    ASCII alphanumerics and ``_.-`` pass through; everything else — Arabic,
    other non-Latin scripts, spaces, emoji, and the glob metacharacters — is
    escaped. Escaping ``*`` and ``?`` matters: a literal one in a tool name must
    not become a wildcard that matches other tools.
    """
    if not remote_name:
        raise MCPNameError("tool name must not be empty")
    return "".join(
        ch if ch in _PLAIN_CHARS else (ch if ch.isascii() and ch in _PLAIN_CHARS else _escape(ch))
        for ch in remote_name
    )


def encode_name_pattern(pattern: str) -> str:
    """Encode a glob so it matches local names.

    Identical to :func:`encode_tool_name_element` except that ``*``, ``?`` and
    the bracket characters survive, because a skill author writes a pattern
    rather than a name. Without this, an Arabic pattern like ``mcp__*__طقس*``
    would never match anything: the registry holds escaped names.
    """
    if not pattern:
        raise MCPNameError("pattern must not be empty")
    return "".join(
        ch if ch in _GLOB_CHARS or ch in _PLAIN_CHARS else _escape(ch) for ch in pattern
    )


def decode_tool_name_element(element: str) -> str:
    """Best-effort reverse of the escape step, for diagnostics.

    Not used to route a call. Returns the escaped text unchanged when it
    contains a malformed escape, and cannot recover a name that was truncated
    to fit the budget — for those, the registration mapping is authoritative.
    """
    out: list[str] = []
    index = 0
    while index < len(element):
        ch = element[index]
        if ch == _ESCAPE and index + 1 + _ESCAPE_WIDTH <= len(element):
            digits = element[index + 1 : index + 1 + _ESCAPE_WIDTH]
            try:
                out.append(chr(int(digits, 36)))
                index += 1 + _ESCAPE_WIDTH
                continue
            except ValueError:
                pass
        out.append(ch)
        index += 1
    return "".join(out)


def namespaced_tool_name(server: str, remote_name: str) -> str:
    """Local tool name for a remote tool, inside the local budget.

    Deterministic: the same inputs always produce the same name, so a scope
    computed at configuration time matches the registry entry built later.
    """
    prefix = f"{NAMESPACE_PREFIX}{NAMESPACE_SEPARATOR}{server}{NAMESPACE_SEPARATOR}"
    element = encode_tool_name_element(remote_name)
    if len(prefix) + len(element) <= MAX_LOCAL_TOOL_NAME:
        return prefix + element
    digest = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:8]
    keep = MAX_LOCAL_TOOL_NAME - len(prefix) - 1 - len(digest)
    return f"{prefix}{element[:keep]}{_ESCAPE}{digest}"


def is_namespaced_tool(name: str) -> bool:
    return str(name).startswith(f"{NAMESPACE_PREFIX}{NAMESPACE_SEPARATOR}")


def split_namespaced_tool(name: str) -> Optional[tuple[str, str]]:
    """Split a local name into ``(server, element)``.

    ``element`` is the *encoded* element, which equals the remote name only when
    that name needed no escaping. Callers that need the remote name must take it
    from the registration, not from this function — see the module docstring.
    """
    prefix = f"{NAMESPACE_PREFIX}{NAMESPACE_SEPARATOR}"
    if not str(name).startswith(prefix):
        return None
    remainder = str(name)[len(prefix) :]
    server, separator, element = remainder.partition(NAMESPACE_SEPARATOR)
    if not separator or not server or not element:
        return None
    return server, element


__all__ = [
    "MAX_LOCAL_TOOL_NAME",
    "MAX_SERVER_NAME",
    "MCPNameError",
    "NAMESPACE_PREFIX",
    "NAMESPACE_SEPARATOR",
    "SERVER_NAME_RE",
    "decode_tool_name_element",
    "encode_name_pattern",
    "encode_tool_name_element",
    "is_namespaced_tool",
    "namespaced_tool_name",
    "split_namespaced_tool",
    "validate_server_name",
]
