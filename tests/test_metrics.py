"""اختبارات /metrics — قرار P3-1 (تدقيق 2026-09-25): تنفيذ مقياس HPA الموثق.

k8s/hpa.yaml يوسّع بناءً على `active_websockets` (Pods metric عبر
prometheus-adapter) — كان موثقًا بلا تنفيذ. هذا الاختبار يمنع عودة الفجوة:
المسار موجود، والعداد يرتفع عند قبول WS ويعود عند الانقطاع.
"""
import re

from fastapi.testclient import TestClient

from nimna.api import metrics as metrics_mod
from nimna.api.app import create_app


def test_metrics_endpoint_public_and_exposes_hpa_gauge(agent):
    client = TestClient(create_app(agent.settings, agent=agent))
    r = client.get("/metrics")  # عام عمداً — Prometheus داخل العنقود، الحمولة عدّادات فقط
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "active_websockets" in r.text
    assert "ws_connections_total" in r.text


def test_ws_lifecycle_moves_active_websockets_gauge(agent):
    client = TestClient(create_app(agent.settings, agent=agent))
    before = metrics_mod.ws_active_value()

    with client.websocket_connect(
        "/ws/sess-metrics-1",
        subprotocols=["nimna.key." + agent.settings.api_keys[0]],
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        during = metrics_mod.ws_active_value()
        assert during == before + 1

    after = metrics_mod.ws_active_value()
    assert after == before  # التنقيص في finally — لا تسريب عدّاد


def test_metrics_body_matches_exposition_format(agent):
    client = TestClient(create_app(agent.settings, agent=agent))
    text = client.get("/metrics").text
    # صيغة exposition صالحة: HELP ثم TYPE ثم القيمة لكل سلسلة
    for name in ("active_websockets", "ws_connections_total"):
        assert re.search(rf"# HELP {name} .+", text), name
        assert re.search(rf"# TYPE {name} (gauge|counter)", text), name
        assert re.search(rf"^{name} [0-9.]+$", text, re.M), name
