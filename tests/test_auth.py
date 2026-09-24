"""Security PR: API-key auth, boot refusal, CORS, rate limits, security headers,
WebSocket auth, UI wiring, docker-compose / deployment hygiene.

Every assertion here is falsifiable: removing the corresponding control in
nimna/api/security.py (or the config files) turns at least one test red.
"""
from __future__ import annotations

import dataclasses
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nimna.api.app import create_app
from nimna.api.security import (
    API_KEY_HEADER,
    DEV_ORIGIN_REGEX,
    WS_KEY_PROTOCOL_PREFIX,
    WS_SUBPROTOCOL,
    ConnectionLimiter,
    SecurityConfig,
    SecurityConfigError,
    SlidingWindowRateLimiter,
    is_public_path,
)
from nimna.config import Settings

REPO = Path(__file__).resolve().parent.parent
KEY = secrets.token_urlsafe(32)
KEY2 = secrets.token_urlsafe(32)
WRONG = secrets.token_urlsafe(32)
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}


def _settings(base: Settings, **overrides) -> Settings:
    defaults = dict(env="production", api_keys=[KEY], allowed_origins=None,
                    chat_rate_limit_per_minute=30, ws_max_connections_per_key=10)
    defaults.update(overrides)
    return dataclasses.replace(base, **defaults)


def _app(agent, **overrides):
    return create_app(_settings(agent.settings, **overrides), agent=agent)


@pytest.fixture
def prod_app(agent):
    return _app(agent, api_keys=[KEY, KEY2])


@pytest.fixture
def client(prod_app):
    return TestClient(prod_app)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _api_routes(app) -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/") and route.path != "/api/health":
            for method in sorted(route.methods - {"HEAD"}):
                out.append((method, _concrete(route.path)))
    return out


def _assert_security_headers(response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, f"{name} missing on {response.request.url}"


# ---------------------------------------------------------------------------
# 1. boot refusal
# ---------------------------------------------------------------------------

def test_production_is_the_default_mode(monkeypatch):
    monkeypatch.delenv("NIMNA_ENV", raising=False)
    assert Settings().env == "production"
    assert Settings.from_env(env_file=None).env == "production"


def test_production_without_key_refuses_to_build(agent):
    with pytest.raises(SecurityConfigError, match="NIMNA_API_KEY is required"):
        _app(agent, api_keys=[])


@pytest.mark.parametrize("bad_key, expected", [
    ("short-key-123", "too short"),
    ("a" * 40, "placeholder / low-entropy"),
    ("x" * 20 + " spaces " + "y" * 20, "characters outside"),
    ("abc/def=" + "Q" * 30, "characters outside"),   # base64 '/' '=' break WS sub-protocols
])
def test_production_rejects_weak_or_malformed_keys_without_echoing_them(agent, bad_key, expected):
    with pytest.raises(SecurityConfigError, match=expected) as exc:
        _app(agent, api_keys=[bad_key])
    assert bad_key not in str(exc.value)


def test_unknown_env_value_fails_closed(agent):
    with pytest.raises(SecurityConfigError, match="NIMNA_ENV"):
        _app(agent, env="staging-ish")


@pytest.mark.parametrize("field, value", [("chat_rate_limit_per_minute", 0), ("ws_max_connections_per_key", 0)])
def test_limits_cannot_be_disabled(agent, field, value):
    with pytest.raises(SecurityConfigError):
        _app(agent, **{field: value})


@pytest.mark.parametrize("name, raw", [("NIMNA_CHAT_RATE_LIMIT", "abc"), ("NIMNA_CHAT_RATE_LIMIT", "3O"),
                                       ("NIMNA_WS_MAX_CONNECTIONS", "10.5"), ("NIMNA_WS_MAX_CONNECTIONS", "ten")])
def test_non_numeric_limits_refuse_boot_instead_of_silent_default(monkeypatch, name, raw):
    monkeypatch.setenv("NIMNA_API_KEY", KEY)
    monkeypatch.setenv(name, raw)
    with pytest.raises(SecurityConfigError, match=f"{name}=.*is not an integer"):
        SecurityConfig.from_settings(Settings.from_env(env_file=None))


def test_numeric_limits_from_env_are_applied(monkeypatch):
    monkeypatch.setenv("NIMNA_API_KEY", KEY)
    monkeypatch.setenv("NIMNA_CHAT_RATE_LIMIT", " 45 ")
    monkeypatch.delenv("NIMNA_WS_MAX_CONNECTIONS", raising=False)
    cfg = SecurityConfig.from_settings(Settings.from_env(env_file=None))
    assert (cfg.chat_rate_limit_per_minute, cfg.ws_max_connections_per_key) == (45, 10)


def _clean_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NIMNA_")}
    env.update(MODEL_PROVIDER="mock", DB_PATH=":memory:", WORKSPACE_DIR=str(tmp_path / "ws"),
               SKILLS_DIR=str(REPO / "skills"), PYTHONPATH=str(REPO))
    env.update(extra)
    return env


def test_uvicorn_import_path_refuses_to_boot_without_key(tmp_path):
    """`uvicorn nimna.api.app:app` / FastAPI Cloud entrypoint must crash, not serve."""
    proc = subprocess.run([sys.executable, "-c", "import nimna.api.app as m; m.app"],
                          cwd=tmp_path, env=_clean_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert "NIMNA_API_KEY is required" in proc.stderr


def test_uvicorn_import_path_boots_with_key(tmp_path):
    proc = subprocess.run([sys.executable, "-c", "import nimna.api.app as m; print(type(m.app).__name__)"],
                          cwd=tmp_path, env=_clean_env(tmp_path, NIMNA_API_KEY=KEY),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "FastAPI" in proc.stdout
    assert KEY not in proc.stdout + proc.stderr


def test_nimna_serve_exits_2_without_key(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "nimna", "serve", "--port", "1"],
                          cwd=tmp_path, env=_clean_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2
    assert "refusing to start" in proc.stderr and "NIMNA_API_KEY" in proc.stderr


def test_doctor_reports_api_security(tmp_path):
    bad = subprocess.run([sys.executable, "-m", "nimna", "doctor", "--offline"], cwd=tmp_path,
                         env=_clean_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert "❌ API security" in bad.stdout and bad.returncode != 0
    good = subprocess.run([sys.executable, "-m", "nimna", "doctor", "--offline"], cwd=tmp_path,
                          env=_clean_env(tmp_path, NIMNA_API_KEY=KEY), capture_output=True, text=True, timeout=120)
    assert "✅ API security: env=production, auth=api-key (1 key)" in good.stdout
    assert KEY not in good.stdout + good.stderr


def test_settings_parse_env_and_never_repr_keys(monkeypatch):
    monkeypatch.setenv("NIMNA_ENV", "Development")
    monkeypatch.setenv("NIMNA_API_KEY", f"{KEY}, {KEY2}")
    monkeypatch.setenv("NIMNA_ALLOWED_ORIGINS", "https://a.example, http://localhost:3000")
    monkeypatch.setenv("NIMNA_CHAT_RATE_LIMIT", "7")
    monkeypatch.setenv("NIMNA_WS_MAX_CONNECTIONS", "3")
    s = Settings.from_env(env_file=None)
    assert s.env == "development"
    assert s.api_keys == [KEY, KEY2]
    assert s.allowed_origins == ["https://a.example", "http://localhost:3000"]
    assert (s.chat_rate_limit_per_minute, s.ws_max_connections_per_key) == (7, 3)
    assert KEY not in repr(s) and KEY2 not in repr(s)
    assert KEY not in repr(SecurityConfig.from_settings(s))
    monkeypatch.delenv("NIMNA_ALLOWED_ORIGINS")
    assert Settings.from_env(env_file=None).allowed_origins is None


# ---------------------------------------------------------------------------
# 2. authentication
# ---------------------------------------------------------------------------

def test_public_paths_work_without_key(client):
    root = client.get("/")
    assert root.status_code == 200 and "<html" in root.text  # static UI keeps working
    assert client.get("/api/health").status_code == 200
    assert client.get("/static/does-not-exist.js").status_code == 404  # public, not 401


def test_every_api_route_requires_a_valid_key(prod_app, client):
    routes = _api_routes(prod_app)
    assert len(routes) >= 24, "route discovery broke — the test would prove nothing"
    for method, path in routes:
        for headers in ({}, {API_KEY_HEADER: WRONG}, {API_KEY_HEADER: ""}, {API_KEY_HEADER: KEY[:-1]}):
            r = client.request(method, path, headers=headers)
            assert r.status_code == 401, f"{method} {path} with {headers or 'no header'} -> {r.status_code}"
            assert r.json()["header"] == API_KEY_HEADER


def test_every_api_route_accepts_a_valid_key(prod_app, client):
    for method, path in _api_routes(prod_app):
        r = client.request(method, path, headers={API_KEY_HEADER: KEY})
        assert r.status_code != 401, f"{method} {path} rejected a valid key"


def test_rotated_second_key_is_accepted(client):
    assert client.get("/api/tools", headers={API_KEY_HEADER: KEY2}).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc", "/unknown", "/api/health/", "/static"])
def test_default_deny_for_everything_not_allow_listed(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("raw_path", ["/static/../api/tools", "/static/./x", "/static/%2e%2e/api/tools"])
def test_raw_dot_segments_never_ride_the_public_prefix(prod_app, raw_path):
    """HTTP clients normalise dot-segments, so drive the ASGI app with the raw path."""
    import asyncio
    from urllib.parse import unquote

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": unquote(raw_path), "raw_path": raw_path.encode(), "query_string": b"",
             "root_path": "", "headers": [(b"host", b"testserver")], "client": ("203.0.113.9", 1),
             "server": ("testserver", 80)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(prod_app(scope, receive, send))
    assert sent[0]["status"] == 401


def test_is_public_path_is_an_exact_allow_list():
    assert is_public_path("/") and is_public_path("/api/health") and is_public_path("/static/app.js")
    for path in ("/static", "/static/", "/static/../api/chat", "/api/healthz", "/api/health/x", "/ws/x", ""):
        assert not is_public_path(path), path


def test_key_is_checked_in_constant_time_primitive():
    source = (REPO / "nimna/api/security.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source


def test_development_without_key_serves_loopback_only(agent):
    app = _app(agent, env="development", api_keys=[])
    local = TestClient(app, client=("127.0.0.1", 40000))
    assert local.get("/api/tools").status_code == 200
    assert TestClient(app, client=("::1", 40000)).get("/api/tools").status_code == 200
    remote = TestClient(app, client=("203.0.113.9", 40000))
    assert remote.get("/api/tools").status_code == 401
    assert TestClient(app).get("/api/tools").status_code == 401  # non-IP client host
    assert remote.get("/").status_code == 200


def test_development_with_key_still_enforces_it(agent):
    app = _app(agent, env="development", api_keys=["dev-key"])
    local = TestClient(app, client=("127.0.0.1", 40000))
    assert local.get("/api/tools").status_code == 401
    assert local.get("/api/tools", headers={API_KEY_HEADER: "dev-key"}).status_code == 200


# ---------------------------------------------------------------------------
# 3. WebSocket auth + connection cap
# ---------------------------------------------------------------------------

def _expect_ws_rejected(client, **kwargs):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/session-1", **kwargs) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_websocket_requires_key(client):
    _expect_ws_rejected(client)
    _expect_ws_rejected(client, headers={API_KEY_HEADER: WRONG})
    _expect_ws_rejected(client, subprotocols=[WS_SUBPROTOCOL, WS_KEY_PROTOCOL_PREFIX + WRONG])


def test_websocket_accepts_header_key(client):
    with client.websocket_connect("/ws/session-1", headers={API_KEY_HEADER: KEY}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_websocket_browser_subprotocol_key_is_never_echoed(client):
    with client.websocket_connect("/ws/session-1",
                                  subprotocols=[WS_SUBPROTOCOL, WS_KEY_PROTOCOL_PREFIX + KEY]) as ws:
        assert ws.accepted_subprotocol == WS_SUBPROTOCOL
        assert ws.receive_json()["type"] == "hello"


def _wait_active(limiter: ConnectionLimiter, identity_key: str, config: SecurityConfig, expected: int) -> None:
    identity = config.identity_for(identity_key)
    deadline = time.monotonic() + 5
    while limiter.active(identity) != expected and time.monotonic() < deadline:
        time.sleep(0.02)
    assert limiter.active(identity) == expected


def test_websocket_connection_cap_is_10_per_key(prod_app, client):
    assert prod_app.state.security.ws_max_connections_per_key == 10
    limiter, cfg = prod_app.state.ws_limiter, prod_app.state.security
    with ExitStack() as stack:
        sockets = []
        for _ in range(10):
            ws = stack.enter_context(client.websocket_connect("/ws/s", headers={API_KEY_HEADER: KEY}))
            assert ws.receive_json()["type"] == "hello"
            sockets.append(ws)
        _wait_active(limiter, KEY, cfg, 10)
        _expect_ws_rejected(client, headers={API_KEY_HEADER: KEY})        # 11th for KEY
        with client.websocket_connect("/ws/s", headers={API_KEY_HEADER: KEY2}) as other:
            assert other.receive_json()["type"] == "hello"                 # KEY2 has its own budget
        sockets[0].close()
        _wait_active(limiter, KEY, cfg, 9)
        with client.websocket_connect("/ws/s", headers={API_KEY_HEADER: KEY}) as again:
            assert again.receive_json()["type"] == "hello"                 # slot freed
    _wait_active(limiter, KEY, cfg, 0)


def test_connection_limiter_unit():
    lim = ConnectionLimiter(2)
    assert lim.acquire("a") and lim.acquire("a") and not lim.acquire("a")
    assert lim.acquire("b")
    lim.release("a")
    assert lim.acquire("a")
    lim.release("a"); lim.release("a"); lim.release("a")
    assert lim.active("a") == 0


# ---------------------------------------------------------------------------
# 4. rate limiting
# ---------------------------------------------------------------------------

def test_chat_rate_limit_30_per_minute_per_key(prod_app, client):
    now = [1000.0]
    prod_app.state.chat_limiter.clock = lambda: now[0]
    assert prod_app.state.chat_limiter.limit == 30
    h1, h2 = {API_KEY_HEADER: KEY}, {API_KEY_HEADER: KEY2}
    # empty body -> 422 from validation; the limiter runs first and counts it
    for i in range(30):
        assert client.post("/api/chat", json={}, headers=h1).status_code == 422, i
    blocked = client.post("/api/chat", json={}, headers=h1)
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["retry-after"]) <= 60
    _assert_security_headers(blocked)
    assert client.post("/api/chat", json={}, headers=h2).status_code == 422   # separate bucket
    assert client.get("/api/tools", headers=h1).status_code == 200            # other routes unaffected
    now[0] += 61
    assert client.post("/api/chat", json={}, headers=h1).status_code == 422   # window slid


def test_unauthenticated_requests_do_not_consume_the_budget(prod_app, client):
    for _ in range(40):
        assert client.post("/api/chat", json={}).status_code == 401
    assert client.post("/api/chat", json={}, headers={API_KEY_HEADER: KEY}).status_code == 422


def test_sliding_window_unit():
    now = [0.0]
    lim = SlidingWindowRateLimiter(3, 60, clock=lambda: now[0])
    assert all(lim.hit("k")[0] for _ in range(3))
    allowed, retry = lim.hit("k")
    assert not allowed and retry == pytest.approx(60)
    now[0] = 30
    assert lim.hit("k") == (False, pytest.approx(30))
    now[0] = 60.01
    assert lim.hit("k")[0]


# ---------------------------------------------------------------------------
# 5. CORS
# ---------------------------------------------------------------------------

def _preflight(client, origin):
    return client.options("/api/chat", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-nimna-key"})


def test_cors_production_default_denies_every_origin(client):
    for origin in ("https://evil.example", "http://localhost:3000"):
        pre = _preflight(client, origin)
        assert pre.status_code == 400 and "access-control-allow-origin" not in pre.headers
        r = client.get("/api/health", headers={"Origin": origin})
        assert "access-control-allow-origin" not in r.headers


def test_cors_explicit_allow_list(agent):
    client = TestClient(_app(agent, allowed_origins=["https://app.example/", "http://localhost:3000"]))
    pre = _preflight(client, "https://app.example")
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == "https://app.example"
    assert "x-nimna-key" in pre.headers["access-control-allow-headers"].lower()
    assert pre.headers.get("access-control-allow-credentials") is None
    _assert_security_headers(pre)
    assert _preflight(client, "https://evil.example").status_code == 400
    # the actual request still needs the key; the 401 is readable by the allowed origin
    r = client.post("/api/chat", json={}, headers={"Origin": "https://app.example"})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "https://app.example"


def test_cors_development_default_is_localhost_only(agent):
    client = TestClient(_app(agent, env="development"))
    for origin in ("http://localhost:3000", "http://127.0.0.1:8001", "https://localhost", "http://[::1]:5173"):
        assert _preflight(client, origin).headers.get("access-control-allow-origin") == origin, origin
    for origin in ("https://evil.example", "http://localhost.evil.example", "http://127.0.0.1.nip.io"):
        assert _preflight(client, origin).status_code == 400, origin
    assert re.match(DEV_ORIGIN_REGEX, "http://localhost:3000")


@pytest.mark.parametrize("origins", [["*"], ["https://*.example.com"], ["https://a.example/path"],
                                     ["ftp://a.example"], ["not-an-origin"], ["https://user@a.example"]])
def test_cors_rejects_wildcards_and_non_origins(agent, origins):
    with pytest.raises(SecurityConfigError, match="NIMNA_ALLOWED_ORIGINS"):
        _app(agent, allowed_origins=origins)


# ---------------------------------------------------------------------------
# 6. security headers
# ---------------------------------------------------------------------------

def test_security_headers_on_every_kind_of_response(client):
    responses = [
        client.get("/"),                                                   # static UI
        client.get("/api/health"),                                         # public JSON
        client.get("/api/tools"),                                          # 401
        client.get("/api/tools", headers={API_KEY_HEADER: KEY}),           # 200
        client.get("/static/nope.css"),                                    # 404
        client.post("/api/chat", json={}, headers={API_KEY_HEADER: KEY}),  # 422
        _preflight(client, "https://evil.example"),                        # 400 preflight
    ]
    for r in responses:
        _assert_security_headers(r)


def test_security_headers_override_weaker_app_values(agent):
    app = _app(agent)

    @app.get("/api/_weak")
    def weak():
        from fastapi.responses import JSONResponse
        return JSONResponse({}, headers={"X-Frame-Options": "ALLOWALL"})

    r = TestClient(app).get("/api/_weak", headers={API_KEY_HEADER: KEY})
    assert r.headers.get_list("x-frame-options") == ["DENY"]


# ---------------------------------------------------------------------------
# 7. web UI wiring (the page on / must keep working behind auth)
# ---------------------------------------------------------------------------

UI = (REPO / "nimna/api/static/index.html").read_text(encoding="utf-8")


def test_ui_routes_every_api_call_through_the_key_wrapper():
    assert "fetch('/api" not in UI and 'fetch("/api' not in UI
    assert UI.count("api('/api") >= 10
    assert "headers['X-Nimna-Key']=k" in UI
    assert "r.status===401" in UI and "askKey(" in UI
    assert "sessionStorage" in UI and "localStorage.setItem(KEY_STORE" not in UI
    assert 'id="keyDlg"' in UI and 'type="password"' in UI


def test_ui_websocket_uses_subprotocol_credential_and_single_socket():
    assert "'nimna.v1','nimna.key.'+k" in UI
    assert "new WebSocket(url, protocols)" in UI
    assert "ws.onclose=null" in UI  # previous socket is detached before reconnecting


def test_ui_script_is_valid_javascript(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = re.findall(r"<script>(.*?)</script>", UI, re.S)[-1]
    js = tmp_path / "ui.js"
    js.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# 8. docker-compose / deployment / .env.example / docs
# ---------------------------------------------------------------------------

COMPOSE_TEXT = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
GUARD = REPO / "infra/desktop/vnc-guard.sh"


def test_compose_vnc_password_comes_from_env_and_is_guarded():
    desktop = COMPOSE["services"]["desktop"]
    env = desktop["environment"]
    assert "VNC_PASSWORD=${VNC_PASSWORD:-}" in env
    assert not any(e.lower().startswith("vnc_password=nimna") for e in env)
    assert "VNC_PASSWORD=nimna" not in COMPOSE_TEXT
    assert desktop["entrypoint"] == ["/bin/sh", "/usr/local/bin/nimna-vnc-guard.sh", "/startup.sh"]
    assert "./infra/desktop/vnc-guard.sh:/usr/local/bin/nimna-vnc-guard.sh:ro" in desktop["volumes"]
    assert desktop["ports"] == ["127.0.0.1:6901:80"]
    # `${VNC_PASSWORD:?}` would break `docker compose up nimna` for everyone
    assert "${VNC_PASSWORD:?" not in COMPOSE_TEXT


@pytest.mark.parametrize("password", ["", "nimna", "NIMNA", "password", "changeme", "ubuntu", "short-pw-11"])
def test_vnc_guard_refuses_weak_passwords(password):
    proc = subprocess.run(["sh", str(GUARD), "echo", "STARTED"], env={"PATH": os.environ["PATH"],
                          "VNC_PASSWORD": password}, capture_output=True, text=True)
    assert proc.returncode == 64 and "STARTED" not in proc.stdout
    assert "refusing to start" in proc.stderr


def test_vnc_guard_refuses_when_unset():
    proc = subprocess.run(["sh", str(GUARD), "echo", "STARTED"], env={"PATH": os.environ["PATH"]},
                          capture_output=True, text=True)
    assert proc.returncode == 64


def test_vnc_guard_hands_over_with_a_strong_password():
    proc = subprocess.run(["sh", str(GUARD), "echo", "STARTED"],
                          env={"PATH": os.environ["PATH"], "VNC_PASSWORD": secrets.token_urlsafe(18)},
                          capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "STARTED"


def test_compose_sandbox_is_marked_dev_only():
    lines = COMPOSE_TEXT.splitlines()
    idx = lines.index("  nimna-sandbox:")
    above = "\n".join(lines[max(0, idx - 20):idx])
    assert "DEV ONLY — لا تنشر" in above
    default = "\n".join(line for line in lines[lines.index("  nimna:"):idx] if not line.lstrip().startswith("#"))
    assert "docker.sock" not in default


def _code_nimna_env_names() -> set[str]:
    text = (REPO / "nimna/config.py").read_text(encoding="utf-8")
    return set(re.findall(r'_env\w*\("(NIMNA_[A-Z_]+)"', text))


def test_env_example_documents_every_new_variable():
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    names = _code_nimna_env_names()
    assert names == {"NIMNA_ENV", "NIMNA_API_KEY", "NIMNA_ALLOWED_ORIGINS",
                     "NIMNA_CHAT_RATE_LIMIT", "NIMNA_WS_MAX_CONNECTIONS"}
    for name in names | {"VNC_PASSWORD"}:
        assert re.search(rf"^{name}=", example, re.M), name
    assert re.search(r"^NIMNA_ENV=production\s*$", example, re.M)
    assert re.search(r"^NIMNA_API_KEY=\s*$", example, re.M)      # never a value in the example
    assert re.search(r"^VNC_PASSWORD=\s*$", example, re.M)
    assert "X-Nimna-Key" in example


def test_deployment_manifests_require_the_key():
    render = yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))
    env = {e["key"]: e for e in render["services"][0]["envVars"]}
    assert env["NIMNA_ENV"]["value"] == "production"
    assert env["NIMNA_API_KEY"] == {"key": "NIMNA_API_KEY", "sync": False}
    k8s = list(yaml.safe_load_all((REPO / "k8s/deployment.yaml").read_text(encoding="utf-8")))
    container = k8s[0]["spec"]["template"]["spec"]["containers"][0]
    kenv = {e["name"]: e for e in container["env"]}
    assert kenv["NIMNA_ENV"]["value"] == "production"
    ref = kenv["NIMNA_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "NIMNA_API_KEY" and not ref.get("optional", False)


def test_ci_scans_the_production_posture():
    ci = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "NIMNA_ENV: production" in ci and "::add-mask::" in ci
    assert 'AUTH GATE BROKEN' in ci
    gate = (REPO / "scripts/production_gate.sh").read_text(encoding="utf-8")
    assert "NIMNA_ENV=production" in gate and "auth gate broken" in gate


def test_security_docs_cover_the_controls():
    doc = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    for needle in ("X-Nimna-Key", "NIMNA_API_KEY", "NIMNA_ENV", "NIMNA_ALLOWED_ORIGINS",
                   "NIMNA_CHAT_RATE_LIMIT", "NIMNA_WS_MAX_CONNECTIONS", "VNC_PASSWORD",
                   "/api/health", "nimna.key.", "X-Frame-Options", "Referrer-Policy", "nosniff"):
        assert needle in doc, needle
    assert "VNC password `nimna`" not in doc
    runbook = (REPO / "docs/API-SECURITY.md").read_text(encoding="utf-8")
    assert "FastAPI Cloud" in runbook and "token_urlsafe(32)" in runbook
