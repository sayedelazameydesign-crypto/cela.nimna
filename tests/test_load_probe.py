"""Load-probe CLI contract (offline — stub HTTP server, no real load).

The probe itself is stdlib-only and runs against real deployments from the
runbook; these tests pin its argument handling, threshold verdicts, and
reporting against an in-thread stub server.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "load_probe.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=str(REPO))


class _Stub(BaseHTTPRequestHandler):
    mode = "ok"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            if _Stub.mode == "flaky" and getattr(self.server, "hits", 0) % 2:
                self._send(500, {"detail": "boom"})
            else:
                self._send(200, {"status": "ok"})
            self.server.hits = getattr(self.server, "hits", 0) + 1
        else:
            self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._send(200, {"run_id": "stub", "status": "done"})

    def log_message(self, *args):  # silence the stub
        pass


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_help_and_usage_errors():
    assert _run("--help").returncode == 0
    assert _run().returncode == 2  # --base-url is required
    assert _run("--base-url", "http://x", "--requests", "0").returncode == 2


def test_healthy_target_passes_within_thresholds():
    server = _serve()
    try:
        result = _run("--base-url", f"http://127.0.0.1:{server.server_port}",
                      "--requests", "10", "--concurrency", "2")
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stderr
    assert "errors=0" in result.stdout
    assert "within thresholds" in result.stdout


def test_error_breach_is_reported_not_hidden():
    _Stub.mode = "flaky"
    server = _serve()
    try:
        result = _run("--base-url", f"http://127.0.0.1:{server.server_port}",
                      "--requests", "6", "--concurrency", "1", "--max-errors", "0")
    finally:
        server.shutdown()
        _Stub.mode = "ok"
    assert result.returncode == 2
    assert "BREACH" in result.stderr


def test_chat_without_key_is_refused():
    server = _serve()
    try:
        result = _run("--base-url", f"http://127.0.0.1:{server.server_port}",
                      "--include-chat", "--key", "")
    finally:
        server.shutdown()
    assert result.returncode == 64
