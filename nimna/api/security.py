"""Access control for the Nimna HTTP / WebSocket API.

Four layers, installed by :func:`install_security` (outermost first):

1. ``SecurityHeadersMiddleware`` – ``X-Content-Type-Options``, ``X-Frame-Options``
   and ``Referrer-Policy`` on *every* HTTP response (including 401/429/404 and
   CORS preflights).
2. Starlette ``CORSMiddleware`` – an explicit allow-list from
   ``NIMNA_ALLOWED_ORIGINS``. Production default: no origin is allowed.
   Development default: ``localhost`` / ``127.0.0.1`` / ``[::1]`` only.
3. ``APIGuardMiddleware`` – default-deny API-key authentication (header
   ``X-Nimna-Key``) for every path except ``/``, ``/api/health`` and
   ``/static/*``; per-key rate limit on ``POST /api/chat`` and a per-key cap on
   concurrent WebSocket connections.
4. The application itself.

Boot policy (enforced by :meth:`SecurityConfig.from_settings`, which
``create_app`` calls before anything else is built):

* ``NIMNA_ENV`` defaults to ``production`` – forgetting it fails *closed*.
* In production ``NIMNA_API_KEY`` is mandatory (>= 32 chars, not a known
  placeholder). Missing / weak => :class:`SecurityConfigError` => the server
  refuses to start.
* In development without a key, only loopback clients are served.

Key values are never logged, echoed, or included in error messages.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, MutableMapping, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

API_KEY_HEADER = "X-Nimna-Key"
_API_KEY_HEADER_RAW = API_KEY_HEADER.lower().encode("latin-1")

# Browsers cannot set custom headers on a WebSocket handshake, so the web UI
# offers two sub-protocols: ``nimna.v1`` (negotiated back) and
# ``nimna.key.<key>`` (carries the credential, never echoed back).
WS_SUBPROTOCOL = "nimna.v1"
WS_KEY_PROTOCOL_PREFIX = "nimna.key."

PUBLIC_EXACT_PATHS = frozenset({"/", "/api/health"})
PUBLIC_PREFIXES = ("/static/",)

PRODUCTION = "production"
DEVELOPMENT = "development"
_ENV_ALIASES = {
    "production": PRODUCTION,
    "prod": PRODUCTION,
    "development": DEVELOPMENT,
    "dev": DEVELOPMENT,
    "local": DEVELOPMENT,
}

MIN_PRODUCTION_KEY_LENGTH = 32
# RFC 7230 ``tchar`` subset: valid in an HTTP header *and* in a WebSocket
# sub-protocol token. ``secrets.token_urlsafe(32)`` only emits these.
_KEY_CHARSET = re.compile(r"[A-Za-z0-9._~+\-]+")
_WEAK_KEYS = frozenset({
    "changeme", "change-me", "change_me", "secret", "password", "nimna",
    "test", "dev", "development", "production", "apikey", "api-key", "api_key",
    "your-api-key", "your_api_key", "replace-me", "replace_me", "xxx",
})

DEV_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$"
CORS_ALLOW_METHODS = ("GET", "POST", "DELETE", "OPTIONS")
CORS_ALLOW_HEADERS = ("Content-Type", API_KEY_HEADER)

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)

WS_POLICY_VIOLATION = 1008


class SecurityConfigError(RuntimeError):
    """Raised when the security configuration is unsafe; the server must not boot."""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _key_identity(key: str) -> str:
    """Stable, non-reversible label for a key (used for rate-limit buckets)."""
    return "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _normalise_origin(raw: str) -> str:
    origin = raw.strip().rstrip("/")
    if origin == "*" or "*" in origin:
        raise SecurityConfigError(
            "NIMNA_ALLOWED_ORIGINS must list explicit origins; wildcards ('*') are refused"
        )
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SecurityConfigError(
            f"NIMNA_ALLOWED_ORIGINS entry {origin!r} is not an origin (expected scheme://host[:port])"
        )
    if parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        raise SecurityConfigError(
            f"NIMNA_ALLOWED_ORIGINS entry {origin!r} must not contain a path, query, fragment or credentials"
        )
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _limit(value: Any, name: str) -> int:
    """Integer limit or boot refusal — a typo must not silently become the default."""
    if isinstance(value, bool):
        raise SecurityConfigError(f"{name} must be an integer")
    try:
        return int(str(value).strip())
    except ValueError:
        raise SecurityConfigError(f"{name}={str(value)[:20]!r} is not an integer") from None


@dataclass(frozen=True)
class SecurityConfig:
    env: str
    api_keys: tuple[str, ...] = field(default=(), repr=False)
    allowed_origins: tuple[str, ...] = ()
    allow_origin_regex: Optional[str] = None
    chat_rate_limit_per_minute: int = 30
    ws_max_connections_per_key: int = 10

    # -- construction ----------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Any) -> "SecurityConfig":
        raw_env = str(getattr(settings, "env", PRODUCTION) or PRODUCTION).strip().lower()
        env = _ENV_ALIASES.get(raw_env)
        if env is None:
            raise SecurityConfigError(
                f"NIMNA_ENV={raw_env!r} is not recognised; use 'production' or 'development'"
            )

        keys: list[str] = []
        for index, raw in enumerate(getattr(settings, "api_keys", None) or [], start=1):
            key = str(raw).strip()
            if not key:
                continue
            if not _KEY_CHARSET.fullmatch(key):
                raise SecurityConfigError(
                    f"NIMNA_API_KEY entry #{index} contains characters outside [A-Za-z0-9._~+-]; "
                    "generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
                )
            if env == PRODUCTION:
                if len(key) < MIN_PRODUCTION_KEY_LENGTH:
                    raise SecurityConfigError(
                        f"NIMNA_API_KEY entry #{index} is too short for production "
                        f"(minimum {MIN_PRODUCTION_KEY_LENGTH} characters)"
                    )
                if key.lower() in _WEAK_KEYS or len(set(key)) < 8:
                    raise SecurityConfigError(
                        f"NIMNA_API_KEY entry #{index} is a placeholder / low-entropy value"
                    )
            if key not in keys:
                keys.append(key)

        if env == PRODUCTION and not keys:
            raise SecurityConfigError(
                "NIMNA_API_KEY is required when NIMNA_ENV=production (the default). "
                "Refusing to start an unauthenticated server. Generate a key with: "
                "python -c \"import secrets; print(secrets.token_urlsafe(32))\" "
                "— or set NIMNA_ENV=development for a loopback-only local run."
            )

        configured = getattr(settings, "allowed_origins", None)
        origin_regex: Optional[str] = None
        if configured is None:
            origins: tuple[str, ...] = ()
            if env == DEVELOPMENT:
                origin_regex = DEV_ORIGIN_REGEX
        else:
            normalised: list[str] = []
            for raw in configured:
                if str(raw).strip():
                    o = _normalise_origin(str(raw))
                    if o not in normalised:
                        normalised.append(o)
            origins = tuple(normalised)

        chat_limit = _limit(getattr(settings, "chat_rate_limit_per_minute", 30), "NIMNA_CHAT_RATE_LIMIT")
        ws_limit = _limit(getattr(settings, "ws_max_connections_per_key", 10), "NIMNA_WS_MAX_CONNECTIONS")
        if chat_limit < 1:
            raise SecurityConfigError("NIMNA_CHAT_RATE_LIMIT must be >= 1 (rate limiting cannot be disabled)")
        if ws_limit < 1:
            raise SecurityConfigError("NIMNA_WS_MAX_CONNECTIONS must be >= 1")

        cfg = cls(env=env, api_keys=tuple(keys), allowed_origins=origins,
                  allow_origin_regex=origin_regex,
                  chat_rate_limit_per_minute=chat_limit,
                  ws_max_connections_per_key=ws_limit)
        if env == DEVELOPMENT and not keys:
            log.warning("NIMNA_ENV=development without NIMNA_API_KEY: API served to loopback clients only")
        return cfg

    # -- behaviour -------------------------------------------------------
    @property
    def auth_mode(self) -> str:
        return "api-key" if self.api_keys else "loopback-only"

    def identity_for(self, presented: Optional[str]) -> Optional[str]:
        """Return the bucket identity of a valid key, else None (constant-time compare)."""
        if not presented or not self.api_keys:
            return None
        candidate = presented.encode("utf-8", "surrogatepass")
        match: Optional[str] = None
        for key in self.api_keys:
            if hmac.compare_digest(candidate, key.encode("utf-8")):
                match = key  # no early exit: timing does not depend on the key index
        return _key_identity(match) if match is not None else None

    def describe(self) -> str:
        if self.allowed_origins:
            cors = f"allow-list ({len(self.allowed_origins)})"
        elif self.allow_origin_regex:
            cors = "localhost only"
        else:
            cors = "deny all"
        auth = f"api-key ({len(self.api_keys)} key{'s' if len(self.api_keys) != 1 else ''})" \
            if self.api_keys else "loopback-only (no key)"
        return (f"env={self.env}, auth={auth}, cors={cors}, "
                f"chat={self.chat_rate_limit_per_minute}/min/key, ws={self.ws_max_connections_per_key}/key")


# ---------------------------------------------------------------------------
# limiters
# ---------------------------------------------------------------------------

class SlidingWindowRateLimiter:
    """``limit`` hits per ``window`` seconds per identity (in-process)."""

    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.limit = int(limit)
        self.window = float(window)
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return "memory"

    def hit(self, identity: str) -> tuple[bool, float]:
        """Record a hit. Returns ``(allowed, retry_after_seconds)``."""
        now = self.clock()
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits.setdefault(identity, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False, max(bucket[0] + self.window - now, 0.0)
            bucket.append(now)
            return True, 0.0


class RedisSlidingWindowRateLimiter:
    """``limit`` hits per ``window`` seconds per identity, shared via Redis.

    Same :meth:`hit` contract as :class:`SlidingWindowRateLimiter`, but the
    buckets live in Redis sorted sets so N replicas behind a load balancer
    enforce ONE limit instead of N independent ones.  The check-and-add runs
    as a single Lua script (atomic — no check-then-set race).

    On ANY Redis error the limiter fails OPEN (allows the request): a cache
    blip must degrade throttling, not take down ``/api/chat``.  Every such
    event is counted (``rate_limiter_errors_total``) and logged — degradation
    is visible, never silent.
    """

    _LUA = """
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
    local count = redis.call('ZCARD', KEYS[1])
    if count >= limit then
        local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
        return {0, oldest[2]}
    end
    redis.call('ZADD', KEYS[1], now, ARGV[4])
    redis.call('PEXPIRE', KEYS[1], math.floor(window * 1000) + 5000)
    return {1, 0}
    """

    def __init__(self, redis_client: Any, limit: int, window: float = 60.0,
                 clock: Callable[[], float] = time.time,
                 key_prefix: str = "nimna:ratelimit:chat:"):
        self._redis = redis_client
        self.limit = int(limit)
        self.window = float(window)
        self.clock = clock
        self.key_prefix = key_prefix
        self._script_hash: Optional[str] = None

    @classmethod
    def from_url(cls, redis_url: str, limit: int, window: float = 60.0) -> "RedisSlidingWindowRateLimiter":
        import redis  # type: ignore  # optional dependency (.[prod])

        client = redis.from_url(redis_url, decode_responses=False,
                                socket_connect_timeout=2, socket_timeout=2)
        return cls(client, limit, window)

    @property
    def backend(self) -> str:
        return "redis"

    def _run_script(self, key: str, now: float, member: str) -> Any:
        if self._script_hash is None:
            self._script_hash = self._redis.script_load(self._LUA)
        try:
            return self._redis.evalsha(self._script_hash, 1, key, now, self.window, self.limit, member)
        except Exception as exc:
            # NOSCRIPT (failover/restart flushed the cache) → reload once.
            if "NOSCRIPT" not in str(exc):
                raise
            self._script_hash = self._redis.script_load(self._LUA)
            return self._redis.evalsha(self._script_hash, 1, key, now, self.window, self.limit, member)

    def hit(self, identity: str) -> tuple[bool, float]:
        """Record a hit. Returns ``(allowed, retry_after_seconds)``."""
        import uuid as _uuid

        now = self.clock()
        key = f"{self.key_prefix}{identity}"
        member = f"{now:.6f}:{_uuid.uuid4().hex[:8]}"
        try:
            allowed, oldest = self._run_script(key, now, member)
        except Exception as exc:
            log.warning("rate limiter Redis error (%s) — failing open", exc.__class__.__name__)
            try:
                from .telemetry import get_registry
                get_registry().inc("rate_limiter_errors_total", {"backend": "redis"})
            except Exception:
                pass
            return True, 0.0
        if int(allowed) == 1:
            return True, 0.0
        return False, max(float(oldest) + self.window - now, 0.0)


class ConnectionLimiter:
    """At most ``limit`` concurrent connections per identity."""

    def __init__(self, limit: int):
        self.limit = int(limit)
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, identity: str) -> bool:
        with self._lock:
            current = self._active.get(identity, 0)
            if current >= self.limit:
                return False
            self._active[identity] = current + 1
            return True

    def release(self, identity: str) -> None:
        with self._lock:
            current = self._active.get(identity, 0) - 1
            if current > 0:
                self._active[identity] = current
            else:
                self._active.pop(identity, None)

    def active(self, identity: str) -> int:
        with self._lock:
            return self._active.get(identity, 0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def is_public_path(path: str) -> bool:
    """Exact allow-list. Dot-segments never qualify (no ``/static/../api``)."""
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    if path in PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) and len(path) > len(prefix) for prefix in PUBLIC_PREFIXES)


def _header(scope: Scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return None


def presented_key(scope: Scope) -> Optional[str]:
    key = _header(scope, _API_KEY_HEADER_RAW)
    if key:
        return key
    if scope.get("type") == "websocket":
        for proto in scope.get("subprotocols") or []:
            if proto.startswith(WS_KEY_PROTOCOL_PREFIX):
                return proto[len(WS_KEY_PROTOCOL_PREFIX):]
    return None


def select_ws_subprotocol(scope: Scope) -> Optional[str]:
    """The only sub-protocol ever negotiated back; the key protocol is never echoed."""
    return WS_SUBPROTOCOL if WS_SUBPROTOCOL in (scope.get("subprotocols") or []) else None


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _send_json(send: Send, status: int, payload: dict[str, Any],
                     extra_headers: Iterable[tuple[bytes, bytes]] = ()) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode("latin-1")),
               (b"cache-control", b"no-store"),
               *extra_headers]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _reject_websocket(receive: Receive, send: Send, reason: str) -> None:
    # consume the handshake event, then refuse it (the server answers HTTP 403)
    message = await receive()
    if message.get("type") != "websocket.connect":
        return
    await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION, "reason": reason})


# ---------------------------------------------------------------------------
# middlewares (pure ASGI – they must see WebSocket scopes too)
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in SECURITY_HEADERS}
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                headers.extend(SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class APIGuardMiddleware:
    def __init__(self, app: ASGIApp, config: SecurityConfig,
                 chat_limiter: Any, ws_limiter: ConnectionLimiter,
                 metrics: Any = None) -> None:
        self.app = app
        self.config = config
        self.chat_limiter = chat_limiter
        self.ws_limiter = ws_limiter
        self.metrics = metrics

    def authenticate(self, scope: Scope) -> Optional[str]:
        if self.config.api_keys:
            return self.config.identity_for(presented_key(scope))
        # development without a key: loopback clients only
        client = scope.get("client")
        host = client[0] if client else None
        return f"anon:{host}" if _is_loopback(host) else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if kind == "http" and is_public_path(path):
            await self.app(scope, receive, send)
            return

        identity = self.authenticate(scope)
        if identity is None:
            if kind == "websocket":
                await _reject_websocket(receive, send, "missing or invalid API key")
            else:
                await _send_json(send, 401, {"detail": "missing or invalid API key",
                                             "header": API_KEY_HEADER})
            return
        # Downstream endpoints (idempotency scoping) read this; the raw key is
        # never stored — identity is a truncated sha256 label (see _key_identity).
        scope["nimna.identity"] = identity

        if kind == "websocket":
            if not self.ws_limiter.acquire(identity):
                await _reject_websocket(receive, send, "too many WebSocket connections for this key")
                return
            try:
                await self.app(scope, receive, send)
            finally:
                self.ws_limiter.release(identity)
            return

        if path == "/api/chat" and scope.get("method") == "POST":
            allowed, retry_after = self.chat_limiter.hit(identity)
            if not allowed:
                limit = self.chat_limiter.limit
                if self.metrics is not None:
                    try:
                        self.metrics.inc("rate_limit_hits_total",
                                         {"path": "/api/chat",
                                          "backend": getattr(self.chat_limiter, "backend", "memory")})
                    except Exception:
                        pass
                await _send_json(send, 429,
                                 {"detail": f"rate limit exceeded: {limit} requests/minute on /api/chat"},
                                 [(b"retry-after", str(max(1, math.ceil(retry_after))).encode("latin-1"))])
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def install_security(app: Any, config: SecurityConfig, *,
                     redis_url: Optional[str] = None, metrics: Any = None) -> None:
    """Register the middlewares. Starlette runs the *last added* outermost.

    When ``redis_url`` is set (and the optional ``redis`` package is
    installed), the ``/api/chat`` rate limit is enforced from Redis so every
    replica shares one bucket; otherwise it stays in-process.  A Redis outage
    degrades to fail-open (counted, logged) — the limiter is never a SPOF.
    """
    from starlette.middleware.cors import CORSMiddleware

    chat_limiter: Any = SlidingWindowRateLimiter(config.chat_rate_limit_per_minute, 60.0)
    if redis_url:
        try:
            chat_limiter = RedisSlidingWindowRateLimiter.from_url(
                redis_url, config.chat_rate_limit_per_minute, 60.0)
        except Exception as exc:
            log.warning("rate limiter: Redis unavailable at boot (%s) — using in-process buckets",
                        exc.__class__.__name__)
    ws_limiter = ConnectionLimiter(config.ws_max_connections_per_key)
    app.state.security = config
    app.state.chat_limiter = chat_limiter
    app.state.ws_limiter = ws_limiter

    app.add_middleware(APIGuardMiddleware, config=config,
                       chat_limiter=chat_limiter, ws_limiter=ws_limiter,
                       metrics=metrics)
    app.add_middleware(CORSMiddleware,
                       allow_origins=list(config.allowed_origins),
                       allow_origin_regex=config.allow_origin_regex,
                       allow_methods=list(CORS_ALLOW_METHODS),
                       allow_headers=list(CORS_ALLOW_HEADERS),
                       allow_credentials=False,
                       max_age=600)
    app.add_middleware(SecurityHeadersMiddleware)
    log.info("API security: %s, rate-limit=%s",
             config.describe(), getattr(chat_limiter, "backend", "memory"))
