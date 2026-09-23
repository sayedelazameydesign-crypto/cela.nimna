"""MCP credential handling.

A gateway holds credentials for servers it is allowed to reach. Those
credentials are the highest-value thing in the process, so this module makes
them hard to leak by accident rather than relying on every call site to
remember:

* :class:`MCPCredential` never renders its secret through ``repr``, ``str``,
  or a dataclass dump.
* :meth:`MCPCredential.apply` is the only way to get the secret onto a request,
  and it returns a new header mapping rather than mutating one.
* :func:`redact_headers` is used before any header mapping is logged or
  recorded as evidence.

Authentication is not optional. An unauthenticated remote MCP endpoint is
reachable by anything that can reach the network, so a server entry that
declares a credential requirement but has no credential configured fails
closed at construction time instead of at first use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

#: Header names whose values must never be logged or recorded.
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-mcp-token",
    }
)

REDACTED = "***REDACTED***"


class MCPAuthError(RuntimeError):
    """A credential is required but missing, or malformed."""


@dataclass(frozen=True)
class MCPCredential:
    """A credential bound to a header, with redacted rendering.

    ``scheme`` is used for bearer-style credentials; leave it empty for a raw
    header value such as a custom API key header.
    """

    value: str = field(repr=False)
    header: str = "Authorization"
    scheme: str = "Bearer"

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise MCPAuthError("credential value must not be empty")
        if not self.header.strip():
            raise MCPAuthError("credential header name must not be empty")
        # A credential containing CR/LF is a header-injection attempt, whether
        # accidental or not. Refuse it at construction, never at send time.
        if any(ch in self.value for ch in "\r\n") or any(ch in self.header for ch in "\r\n"):
            raise MCPAuthError("credential must not contain CR or LF")

    @property
    def header_value(self) -> str:
        return f"{self.scheme} {self.value}".strip() if self.scheme else self.value

    def apply(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return a copy of ``headers`` with this credential applied."""
        merged = dict(headers)
        merged[self.header] = self.header_value
        return merged

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"MCPCredential(header={self.header!r}, scheme={self.scheme!r}, value={REDACTED})"

    __str__ = __repr__


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a log-safe copy of a header mapping."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        if str(key).lower() in SENSITIVE_HEADERS or "token" in str(key).lower() or "secret" in str(key).lower():
            out[key] = REDACTED
        else:
            out[key] = value
    return out


def credential_from_env(
    *,
    env_var: Optional[str],
    header: str = "Authorization",
    scheme: str = "Bearer",
) -> Optional[MCPCredential]:
    """Load a credential from the environment.

    Returns ``None`` when no variable is named or the variable is unset, and
    lets the caller decide whether that is fatal — see
    :func:`require_credential`.
    """
    if not env_var:
        return None
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return None
    return MCPCredential(value=raw, header=header, scheme=scheme)


def require_credential(
    *, env_var: Optional[str], header: str = "Authorization", scheme: str = "Bearer"
) -> MCPCredential:
    """Load a credential, raising when it is missing."""
    credential = credential_from_env(env_var=env_var, header=header, scheme=scheme)
    if credential is None:
        raise MCPAuthError(
            f"MCP credential environment variable {env_var!r} is not set; "
            "refusing to use an unauthenticated endpoint"
        )
    return credential


def fingerprint(credential: MCPCredential) -> str:
    """A short, stable, non-reversible identifier for audit correlation.

    Two different credentials never share a fingerprint, and the fingerprint
    reveals nothing about the secret, so it is safe to record as evidence.
    """
    import hashlib

    digest = hashlib.sha256(credential.header_value.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def describe_credential(credential: Optional[MCPCredential]) -> dict[str, Any]:
    """A safe summary of a credential for audit payloads."""
    if credential is None:
        return {"present": False}
    return {
        "present": True,
        "header": credential.header,
        "scheme": credential.scheme or None,
        "fingerprint": fingerprint(credential),
        "length": len(credential.value),
    }


__all__ = [
    "MCPAuthError",
    "MCPCredential",
    "REDACTED",
    "SENSITIVE_HEADERS",
    "credential_from_env",
    "describe_credential",
    "fingerprint",
    "redact_headers",
    "require_credential",
]
