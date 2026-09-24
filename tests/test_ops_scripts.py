"""scripts/preflight-prod-check.sh (against a real local uvicorn server) and
scripts/rollback-prod.sh (against a fake `gh` that serves GitHub API fixtures)."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PREFLIGHT = REPO / "scripts" / "preflight-prod-check.sh"
ROLLBACK = REPO / "scripts" / "rollback-prod.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# preflight — real server
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    if shutil.which("curl") is None:
        pytest.skip("curl required")
    tmp = tmp_path_factory.mktemp("preflight")
    keys = [secrets.token_urlsafe(32) for _ in range(4)]   # one fresh rate-limit bucket per test
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("NIMNA_")}
    env.update(MODEL_PROVIDER="mock", DB_PATH=str(tmp / "x.db"), WORKSPACE_DIR=str(tmp / "ws"),
               SKILLS_DIR=str(REPO / "skills"), PYTHONPATH=str(REPO),
               NIMNA_ENV="production", NIMNA_API_KEY=",".join(keys))
    log = open(tmp / "uvicorn.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "nimna.api.app:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if subprocess.run(["curl", "-sf", f"{base}/api/health"], capture_output=True).returncode == 0:
            break
        if proc.poll() is not None:
            pytest.fail("uvicorn exited: " + (tmp / "uvicorn.log").read_text())
        time.sleep(0.3)
    else:
        proc.terminate()
        pytest.fail("uvicorn never became healthy")
    yield base, keys, tmp / "uvicorn.log"
    proc.terminate()
    proc.wait(timeout=10)
    log.close()


def _preflight(*args: str, key: str | None, **extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in {"NIMNA_API_KEY", "NIMNA_CHAT_RATE_LIMIT"}}
    if key is not None:
        env["NIMNA_API_KEY"] = key
    env.update(extra)
    return subprocess.run(["bash", str(PREFLIGHT), *args], env=env, capture_output=True, text=True, timeout=180)


def test_preflight_passes_against_a_correct_server(live_server):
    base, keys, server_log = live_server
    r = _preflight(base, key=keys[0])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: 16 passed, 0 failed" in r.stdout
    for line in ("GET / -> 200", "GET /api/health -> 200 + JSON status=ok", "GET /api/chat without key -> 401",
                 "GET /api/chat with valid key -> 405", "GET /api/chat with wrong key -> 401",
                 "X-Frame-Options: DENY", "request 31 -> 429, Retry-After:"):
        assert line in r.stdout, line
    assert "FAIL" not in r.stdout
    for key in keys:
        assert key not in r.stdout + r.stderr + server_log.read_text()


def test_preflight_fails_with_a_wrong_key(live_server):
    base, _keys, _ = live_server
    r = _preflight(base, key="wrong-" + secrets.token_urlsafe(32))
    assert r.returncode == 1
    assert "FAIL  GET /api/chat with valid key -> 401" in r.stdout


def test_preflight_fails_when_rate_limit_is_not_where_expected(live_server):
    base, keys, _ = live_server
    r = _preflight(base, "--chat-limit", "20", key=keys[1])
    assert r.returncode == 1
    assert "request 21 -> 422 (expected 429" in r.stdout


def test_preflight_fails_when_the_bucket_is_already_spent(live_server):
    base, keys, _ = live_server
    assert _preflight(base, key=keys[2]).returncode == 0
    again = _preflight(base, key=keys[2])
    assert again.returncode == 1 and "429 already at request 1/30" in again.stdout


def test_preflight_network_error_is_fatal():
    r = _preflight(f"http://127.0.0.1:{_free_port()}", key="k")
    assert r.returncode == 2 and "network/TLS error" in r.stderr


def test_preflight_tls_error_is_fatal(live_server):
    base, keys, _ = live_server
    r = _preflight(base.replace("http://", "https://"), key=keys[3])   # plain-HTTP server over TLS
    assert r.returncode == 2 and "curl exit 35" in r.stderr


@pytest.mark.parametrize("args, key, message", [
    (("https://example.com",), None, "NIMNA_API_KEY is not set"),
    (("http://example.com",), "k", "use https://"),
    ((), "k", "missing <base-url>"),
    (("https://example.com", "--chat-limit", "0"), "k", "--chat-limit must be >= 1"),
    (("https://example.com", "--bogus"), "k", "unknown option"),
])
def test_preflight_usage_errors_exit_64(args, key, message):
    r = _preflight(*args, key=key)
    assert r.returncode == 64 and message in r.stderr


def test_preflight_never_disables_tls_or_leaks_the_key_via_argv():
    src = PREFLIGHT.read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"(^|\s)(-k|--insecure)(\s|$)", code)
    assert "|| true" not in code and "set -Eeuo pipefail" in code
    assert '-H "@$WORK/key.hdr"' in code and "umask 077" in code
    assert '"X-Nimna-Key: $NIMNA_API_KEY"' not in code.replace("printf 'X-Nimna-Key: %s", "")


# ---------------------------------------------------------------------------
# rollback — fake gh
# ---------------------------------------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
fx = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
args = sys.argv[1:]
if not args or args[0] != "api":
    sys.stderr.write("fake gh: only `gh api` is supported\n"); sys.exit(1)
method, body, paginate, path, i, args = "GET", None, False, None, 0, args[1:]
while i < len(args):
    a = args[i]
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "--paginate": paginate = True; i += 1; continue
    if a == "--input": body = json.load(open(args[i + 1])); i += 2; continue
    path = a; i += 1
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps({"method": method, "path": path, "body": body}) + "\n")
key = f"{method} {path}"
if key not in fx:
    sys.stderr.write(f"gh: Not Found (HTTP 404) [{key}]\n"); sys.exit(1)
resp = fx[key]
if paginate and isinstance(resp, list) and len(resp) > 1:      # emulate two pages: `[..][..]`
    h = len(resp) // 2; print(json.dumps(resp[:h])); print(json.dumps(resp[h:]))
else:
    print(json.dumps(resp))
'''

R = "acme/app"
ENV = "Production – app"


def _sha(n: int) -> str:
    return f"{n:07d}" + "a" * 33


def _fixture(deployments, *, head_tree="tree-head", extra=None):
    """deployments: newest first, (id, sha_n, env, [states newest-first])."""
    fx = {f"GET repos/{R}": {"full_name": R, "default_branch": "main"},
          f"GET repos/{R}/deployments?per_page=100": [
              {"id": d, "sha": _sha(s), "environment": e, "created_at": f"2026-09-2{d % 10}T00:00:00Z"}
              for d, s, e, _ in deployments]}
    for d, _s, _e, states in deployments:
        fx[f"GET repos/{R}/deployments/{d}/statuses?per_page=100"] = [{"state": st} for st in states]
    fx[f"GET repos/{R}/git/ref/heads/main"] = {"object": {"sha": _sha(99)}}
    fx[f"GET repos/{R}/git/commits/{_sha(99)}"] = {"sha": _sha(99), "tree": {"sha": head_tree}}
    for _d, s, _e, _st in deployments:
        fx[f"GET repos/{R}/git/commits/{_sha(s)}"] = {"sha": _sha(s), "tree": {"sha": f"tree-{s}"}}
    fx.update(extra or {})
    return fx


WRITES = {
    f"POST repos/{R}/git/commits": {"sha": _sha(100)},
    f"POST repos/{R}/git/refs": {"ref": "refs/heads/rollback/x"},
    f"POST repos/{R}/pulls": {"html_url": f"https://github.com/{R}/pull/7"},
}


def _rollback(tmp_path, fixture, *args):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "fx.json").write_text(json.dumps(fixture), encoding="utf-8")
    log = tmp_path / "gh.log"
    log.write_text("")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               FAKE_GH_FIXTURE=str(tmp_path / "fx.json"), FAKE_GH_LOG=str(log))
    r = subprocess.run(["bash", str(ROLLBACK), "--repo", R, *args], env=env, capture_output=True,
                       text=True, timeout=60)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return r, calls


def _writes(calls):
    return [c for c in calls if c["method"] != "GET"]


def test_rollback_newest_success_targets_previous_distinct_success(tmp_path):
    fx = _fixture([(40, 3, ENV, ["success", "in_progress"]),
                   (30, 3, ENV, ["inactive", "success"]),          # redeploy of the same SHA: skipped
                   (20, 2, ENV, ["inactive", "success", "queued"]),  # superseded => inactive, still counts
                   (10, 1, ENV, ["inactive", "success"])])
    r, calls = _rollback(tmp_path, fx)
    assert r.returncode == 0, r.stderr
    assert f"ROLLBACK TARGET SHA: {_sha(2)}" in r.stdout
    assert f"live:        {_sha(3)[:12]}" in r.stdout
    assert "dry run — nothing changed" in r.stdout
    assert _writes(calls) == []


def test_rollback_newest_failure_targets_last_success(tmp_path):
    fx = _fixture([(50, 4, ENV, ["failure", "in_progress"]),
                   (40, 3, ENV, ["success"]),
                   (30, 2, ENV, ["inactive", "success"])])
    r, _ = _rollback(tmp_path, fx)
    assert r.returncode == 0, r.stderr
    assert f"ROLLBACK TARGET SHA: {_sha(3)}" in r.stdout
    assert "newest attempt failed" in r.stdout


def test_rollback_without_any_success_fails(tmp_path):
    fx = _fixture([(20, 2, ENV, ["failure"]), (10, 1, ENV, ["error", "in_progress"])])
    r, _ = _rollback(tmp_path, fx)
    assert r.returncode == 1 and "no successful deployment" in r.stderr


def test_rollback_with_only_the_live_success_fails(tmp_path):
    fx = _fixture([(20, 2, ENV, ["success"]), (10, 2, ENV, ["inactive", "success"])])
    r, _ = _rollback(tmp_path, fx)
    assert r.returncode == 1 and "no earlier successful deployment" in r.stderr


def test_rollback_requires_env_when_ambiguous_and_filters_by_it(tmp_path):
    fx = _fixture([(40, 4, "Preview", ["success"]), (30, 3, ENV, ["success"]), (20, 2, ENV, ["inactive", "success"])])
    r, _ = _rollback(tmp_path, fx)
    assert r.returncode == 64 and '--env "Preview"' in r.stderr and f'--env "{ENV}"' in r.stderr
    r, _ = _rollback(tmp_path, fx, "--env", ENV)
    assert r.returncode == 0 and f"ROLLBACK TARGET SHA: {_sha(2)}" in r.stdout


def test_rollback_execute_opens_a_pr_restoring_the_exact_tree(tmp_path):
    fx = _fixture([(40, 3, ENV, ["success"]), (20, 2, ENV, ["inactive", "success"])], extra=WRITES)
    r, calls = _rollback(tmp_path, fx, "--execute")
    assert r.returncode == 0, r.stderr
    writes = _writes(calls)
    assert [w["path"] for w in writes] == [f"repos/{R}/git/commits", f"repos/{R}/git/refs", f"repos/{R}/pulls"]
    commit, ref, pr = (w["body"] for w in writes)
    assert commit["tree"] == "tree-2" and commit["parents"] == [_sha(99)]      # on top of main, no rewrite
    assert _sha(2) in commit["message"]
    assert re.fullmatch(r"refs/heads/rollback/0000002-\d{14}", ref["ref"]) and ref["sha"] == _sha(100)
    assert pr["base"] == "main" and pr["head"] == ref["ref"].removeprefix("refs/heads/")
    assert f"https://github.com/{R}/pull/7" in r.stdout
    assert not any(w["method"] in {"PATCH", "PUT", "DELETE"} for w in writes)  # never force-updates main


def test_rollback_execute_noop_when_main_already_has_the_target_tree(tmp_path):
    fx = _fixture([(40, 3, ENV, ["success"]), (20, 2, ENV, ["inactive", "success"])],
                  head_tree="tree-2", extra=WRITES)
    r, calls = _rollback(tmp_path, fx, "--execute")
    assert r.returncode == 0 and "no PR needed" in r.stdout
    assert _writes(calls) == []


def test_rollback_api_errors_are_fatal(tmp_path):
    fx = _fixture([(40, 3, ENV, ["success"]), (20, 2, ENV, ["inactive", "success"])])   # no POST fixtures
    r, _ = _rollback(tmp_path, fx, "--execute")
    assert r.returncode == 2 and "gh api -X POST" in r.stderr
    fx.pop(f"GET repos/{R}/deployments/40/statuses?per_page=100")
    r, _ = _rollback(tmp_path, fx)
    assert r.returncode == 2 and "HTTP 404" in r.stderr


def test_rollback_script_hygiene():
    src = ROLLBACK.read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert "set -Eeuo pipefail" in code and "|| true" not in code
    assert "--force" not in code and "git push" not in code
    assert "python3 -I" in code        # helper cannot be shadowed by a local json.py


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------

def test_api_security_doc_has_pre_merge_checklist_and_env_table():
    doc = (REPO / "docs/API-SECURITY.md").read_text(encoding="utf-8")
    assert "## ما قبل الدمج" in doc
    section = doc.split("## ما قبل الدمج", 1)[1]
    assert section.count("- [ ]") >= 10
    for needle in ("preflight-prod-check.sh", "rollback-prod.sh", "FastAPI Cloud", "Render", "Kubernetes"):
        assert needle in section, needle
    config = (REPO / "nimna/config.py").read_text(encoding="utf-8")
    for name in set(re.findall(r'_env\w*\("(NIMNA_[A-Z_]+)"', config)) | {"VNC_PASSWORD"}:
        assert f"| `{name}` |" in doc, name


def test_env_example_boots_under_the_strict_limit_parser(tmp_path, monkeypatch):
    """`.env.example` has inline comments after the limits; they must not turn into boot refusals."""
    from nimna.api.security import SecurityConfig
    from nimna.config import Settings

    for name in [n for n in os.environ if n.startswith("NIMNA_")]:
        monkeypatch.delenv(name)
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^NIMNA_API_KEY=.*$", "NIMNA_API_KEY=" + secrets.token_urlsafe(32), text)
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    saved = dict(os.environ)
    try:                                   # load_dotenv writes into os.environ: undo it
        cfg = SecurityConfig.from_settings(Settings.from_env(env_file=str(tmp_path / ".env")))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert (cfg.chat_rate_limit_per_minute, cfg.ws_max_connections_per_key) == (30, 10)
