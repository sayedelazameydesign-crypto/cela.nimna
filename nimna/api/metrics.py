"""مقاييس Prometheus للـAPI — قرار P3-1 (تدقيق 2026-09-25).

`/metrics` يكشف مقياس HPA المخصص `active_websockets` الذي كان موثقًا في
k8s/hpa.yaml بلا أي تنفيذ (الفجوة المعمارية الأعلى المتبقية). prometheus_client
تبعية [infra]: عند غيابه يتحول العرض إلى exposition يدوي — التدهور معلن
لا صامت، والعدّاد يبقى صادقًا في الحالتين.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()

try:  # [infra] extra — الغياب ممكن في التثبيتات الدنيا؛ التدهور موثق أدناه
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

    _HAVE_PROMETHEUS = True
except Exception:  # pragma: no cover — بيئة بلا [infra]
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    Counter = Gauge = None  # type: ignore[assignment]
    _HAVE_PROMETHEUS = False


class _ManualSeries:
    """سلسلة gauge/counter يدوية بغياب prometheus_client — نفس الواجهة (inc/dec)."""

    def __init__(self, name: str, help_text: str, kind: str) -> None:
        self.name = name
        self.help_text = help_text
        self.kind = kind
        self._value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        with _LOCK:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with _LOCK:
            self._value -= amount

    @property
    def value(self) -> float:
        with _LOCK:
            return self._value


if _HAVE_PROMETHEUS:
    active_websockets = Gauge("active_websockets", "Currently connected dashboard websockets")
    ws_connections_total = Counter(
        "ws_connections_total", "Dashboard websocket connections accepted")
else:
    active_websockets = _ManualSeries(
        "active_websockets", "Currently connected dashboard websockets", "gauge")
    ws_connections_total = _ManualSeries(
        "ws_connections_total", "Dashboard websocket connections accepted", "counter")
    _SERIES: list[_ManualSeries] = [active_websockets, ws_connections_total]


def ws_active_value() -> float:
    """قراءة موحدة لقيمة active_websockets (للاختبارات وللعرض اليدوي)."""
    if _HAVE_PROMETHEUS:
        return float(active_websockets._value.get())  # type: ignore[attr-defined]
    return active_websockets.value  # type: ignore[return-value]


def render_metrics() -> tuple[bytes, str]:
    """(الجسم، Content-Type) بصيغة exposition — عبر المكتبة أو يدويًا."""
    if _HAVE_PROMETHEUS:
        return generate_latest(), CONTENT_TYPE_LATEST
    lines: list[str] = []
    for s in _SERIES:
        lines += [
            f"# HELP {s.name} {s.help_text}",
            f"# TYPE {s.name} {s.kind}",
            f"{s.name} {s.value:g}",
        ]
    return ("\n".join(lines) + "\n").encode("utf-8"), CONTENT_TYPE_LATEST
