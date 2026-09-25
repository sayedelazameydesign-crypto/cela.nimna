"""Request IDs, Prometheus metrics, and cache policy (pure ASGI, no new deps).

Installed by :func:`install_telemetry` **after** :func:`install_security`, so it
is the outermost layer: even 401/429 rejections get a request ID and are
counted.  Everything here is per-process by design — with N replicas behind a
load balancer, Prometheus scrapes each pod and aggregates (``sum by``).  The
``/metrics`` endpoint itself stays behind API-key auth like every other
non-public path.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, MutableMapping, Optional

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REQUEST_ID_HEADER = "x-request-id"
SCOPE_REQUEST_ID = "nimna.request_id"
_INCOMING_ID = re.compile(r"[\w\-.]{1,128}\Z")

# Path segments that are identifiers (collapsed so metric labels stay bounded).
_DYNAMIC_PREFIXES = (
    "/api/sessions/",
    "/api/approvals/",
    "/api/runs/",
    "/api/skills/",
    "/api/memories/",
)


def normalize_path(path: str) -> str:
    """Collapse identifier segments to ``:id`` for stable metric labels."""
    if not path.startswith("/api/"):
        return path if path in {"/", "/api/health"} else "/other"
    if path == "/api/skills/reload":
        return path
    for prefix in _DYNAMIC_PREFIXES:
        if path.startswith(prefix) and len(path) > len(prefix):
            rest = path[len(prefix):]
            first, _, tail = rest.partition("/")
            if first:
                collapsed = prefix + ":id" + ("/" + tail if tail else "")
                return collapsed
    return path


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


class MetricsRegistry:
    """Thread-safe counters + duration summaries with Prometheus rendering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._durations: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}
        self.started_at = time.time()

    @staticmethod
    def _key(name: str, labels: Optional[dict[str, str]]) -> tuple[str, tuple[tuple[str, str], ...]]:
        items = tuple(sorted((labels or {}).items()))
        return (name, items)

    def inc(self, name: str, labels: Optional[dict[str, str]] = None, amount: float = 1.0) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def observe(self, name: str, labels: Optional[dict[str, str]], seconds: float) -> None:
        """Record a duration sample (rendered as ``_sum`` / ``_count``)."""
        key = self._key(name, labels)
        with self._lock:
            entry = self._durations.setdefault(key, [0.0, 0.0])
            entry[0] += seconds
            entry[1] += 1.0

    def get(self, name: str, labels: Optional[dict[str, str]] = None) -> float:
        with self._lock:
            return self._counters.get(self._key(name, labels), 0.0)

    def render_prometheus(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            durations = {k: list(v) for k, v in self._durations.items()}
            uptime = time.time() - self.started_at
        lines = [
            "# HELP process_uptime_seconds Seconds since this process started.",
            "# TYPE process_uptime_seconds gauge",
            f"process_uptime_seconds {_format_value(uptime)}",
        ]
        by_name: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = {}
        for (name, labels), value in sorted(counters.items()):
            by_name.setdefault(name, []).append((labels, value))
        for name in sorted(by_name):
            lines.append(f"# TYPE {name} counter")
            for labels, value in by_name[name]:
                rendered = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels)
                lines.append(f"{name}{{{rendered}}} {_format_value(value)}" if rendered else f"{name} {_format_value(value)}")
        by_dur: dict[str, list[tuple[tuple[tuple[str, str], ...], list[float]]]] = {}
        for (name, labels), value in sorted(durations.items()):
            by_dur.setdefault(name, []).append((labels, value))
        for name in sorted(by_dur):
            lines.append(f"# TYPE {name}_seconds summary")
            for labels, (total, count) in by_dur[name]:
                rendered = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels)
                suffix = f"{{{rendered}}}" if rendered else ""
                lines.append(f"{name}_seconds_sum{suffix} {_format_value(total)}")
                lines.append(f"{name}_seconds_count{suffix} {_format_value(count)}")
        return "\n".join(lines) + "\n"


_REGISTRY = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    return _REGISTRY


def reset_registry() -> MetricsRegistry:
    """Replace the process registry (tests only — never in production code)."""
    global _REGISTRY  # noqa: PLW0603
    _REGISTRY = MetricsRegistry()
    return _REGISTRY


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class TelemetryMiddleware:
    """Outermost ASGI layer: request ID + metrics + cache policy."""

    def __init__(self, app: ASGIApp, registry: Optional[MetricsRegistry] = None) -> None:
        self.app = app
        self.registry = registry or get_registry()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = None
        for key, value in scope.get("headers") or []:
            if key.lower() == REQUEST_ID_HEADER.encode("latin-1"):
                candidate = value.decode("latin-1").strip()
                if _INCOMING_ID.fullmatch(candidate):
                    request_id = candidate
                break
        request_id = request_id or new_request_id()
        scope[SCOPE_REQUEST_ID] = request_id

        method = scope.get("method", "?")
        path = scope.get("path", "")
        started = time.monotonic()

        async def send_with_telemetry(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = str(message.get("status", 0))
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != REQUEST_ID_HEADER.encode("latin-1")]
                headers.append((REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")))
                # Cache policy: API responses are per-key dynamic data — never
                # stored anywhere; the UI shell may be revalidated, not reused
                # blindly; static assets (if ever mounted) cache for an hour.
                names = {k.lower() for k, _ in headers}
                if b"cache-control" not in names:
                    if path.startswith("/api/"):
                        headers.append((b"cache-control", b"no-store"))
                    elif path == "/":
                        headers.append((b"cache-control", b"no-cache"))
                    elif path.startswith("/static/") and status == "200":
                        headers.append((b"cache-control", b"public, max-age=3600"))
                message = {**message, "headers": headers}
                elapsed = time.monotonic() - started
                template = normalize_path(path)
                self.registry.inc("http_requests_total",
                                  {"method": method, "path": template, "status": status})
                self.registry.observe("http_request_duration", {"method": method, "path": template}, elapsed)
            await send(message)

        await self.app(scope, receive, send_with_telemetry)


def install_telemetry(app: Any, registry: Optional[MetricsRegistry] = None) -> MetricsRegistry:
    """Add the telemetry middleware (call AFTER install_security) and wire the
    provider breaker's transitions into the registry. Returns the registry."""
    reg = registry or get_registry()
    app.state.metrics = reg
    agent = getattr(app.state, "agent", None)
    breaker = getattr(getattr(agent, "provider", None), "circuit_breaker", None)
    if breaker is not None:
        breaker.on_transition.append(
            lambda name, from_state, to_state: reg.inc(
                "breaker_transitions_total",
                {"breaker": name, "from_state": from_state, "to_state": to_state},
            )
        )
    app.add_middleware(TelemetryMiddleware, registry=reg)
    log.info("telemetry: request-id + Prometheus metrics + cache policy installed")
    return reg
