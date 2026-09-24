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


# ---------------------------------------------------------------------------
# rollback after PR #20 merges — the real deployment shape of "Production – celanimna-3ffa6b22"
# ---------------------------------------------------------------------------

LIVE = 55  # stands for 55ef6d6, today's only successful deployment


def test_rollback_has_a_target_after_merge_even_if_the_new_deploy_fails(tmp_path):
    fx = _fixture([(70, 20, ENV, ["failure", "in_progress"]),          # PR #20 deploy fails
                   (60, LIVE, ENV, ["success", "in_progress"])])       # FastAPI Cloud keeps it live
    r, _ = _rollback(tmp_path, fx)
    assert r.returncode == 0, r.stderr
    assert f"ROLLBACK TARGET SHA: {_sha(LIVE)}" in r.stdout and "newest attempt failed" in r.stdout


def test_rollback_has_a_target_after_merge_if_the_new_deploy_succeeds(tmp_path):
    fx = _fixture([(70, 20, ENV, ["success", "in_progress"]),
                   (60, LIVE, ENV, ["inactive", "success"])])
    r, _ = _rollback(tmp_path, fx)
    assert r.returncode == 0 and f"ROLLBACK TARGET SHA: {_sha(LIVE)}" in r.stdout


def test_rollback_has_no_target_today(tmp_path):
    r, _ = _rollback(tmp_path, _fixture([(60, LIVE, ENV, ["success", "in_progress"])]))
    assert r.returncode == 1 and "no earlier successful deployment" in r.stderr


# ---------------------------------------------------------------------------
# git fixtures for emergency-revert / snapshot (local bare "origin", no network)
# ---------------------------------------------------------------------------

def _git(cwd, *args, env=None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


class Repo:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        (tmp / "home").mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=str(tmp / "home"), GIT_CONFIG_NOSYSTEM="1")
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        _git(tmp, "init", "-q", "--bare", str(self.origin), env=self.env)
        _git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main", env=self.env)
        _git(tmp, "clone", "-q", str(self.origin), str(self.work), env=self.env)
        self.git("config", "user.email", "dev@example.com")
        self.git("config", "user.name", "dev")
        self.git("checkout", "-q", "-b", "main")

    def git(self, *args) -> str:
        return _git(self.work, *args, env=self.env)

    def commit(self, msg: str, **files: str) -> str:
        for name, text in files.items():
            (self.work / name).write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def push(self):
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")

    def tree(self, rev: str) -> str:
        return self.git("rev-parse", f"{rev}^{{tree}}")

    def remote_branches(self) -> list[str]:
        out = _git(self.tmp, "ls-remote", "--heads", str(self.origin), env=self.env)
        return sorted(line.split("\t")[1] for line in out.splitlines())

    def merged_feature(self) -> tuple[str, str]:
        """main: base -> merge commit of a 'feature' branch (like GitHub 'Create a merge commit')."""
        base = self.commit("base", **{"app.txt": "v1\n", "README": "r\n"})
        self.git("checkout", "-q", "-b", "feature")
        self.commit("feature: auth", **{"app.txt": "v2-auth\n", "auth.txt": "keys\n"})
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #20 from x/feature")
        self.push()
        return base, self.git("rev-parse", "HEAD")


def _run(script: Path, cwd: Path, *args: str, env: dict, fixture: dict | None = None, tmp: Path | None = None):
    env = dict(env)
    calls_log = None
    if fixture is not None:
        bindir = tmp / "fakebin"
        bindir.mkdir(exist_ok=True)
        (bindir / "gh").write_text(FAKE_GH, encoding="utf-8")
        (bindir / "gh").chmod(0o755)
        (tmp / "fx.json").write_text(json.dumps(fixture), encoding="utf-8")
        calls_log = tmp / "gh.log"
        calls_log.write_text("")
        env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", FAKE_GH_FIXTURE=str(tmp / "fx.json"),
                   FAKE_GH_LOG=str(calls_log))
    r = subprocess.run(["bash", str(script), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    calls = [json.loads(x) for x in calls_log.read_text().splitlines()] if calls_log else []
    return r, calls


REVERT = REPO / "scripts" / "emergency-revert.sh"
SNAPSHOT = REPO / "scripts" / "snapshot-prod.sh"
PR_OK = {f"POST repos/{R}/pulls": {"html_url": f"https://github.com/{R}/pull/21"}}


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git required")
    return Repo(tmp_path)


def _no_worktrees_left(repo: Repo) -> bool:
    return len(repo.git("worktree", "list").splitlines()) == 1


# ---------------------------------------------------------------------------
# emergency-revert
# ---------------------------------------------------------------------------

def test_emergency_revert_dry_run_changes_nothing(repo, tmp_path):
    base, merge = repo.merged_feature()
    head_before, branches_before = repo.git("rev-parse", "HEAD"), repo.remote_branches()
    r, _ = _run(REVERT, repo.work, merge, env=repo.env)
    assert r.returncode == 0, r.stderr
    assert "merge commit (reverted with -m 1" in r.stdout
    assert f"IDENTICAL to {base[:12]}" in r.stdout and "DRY RUN" in r.stdout
    assert repo.remote_branches() == branches_before == ["refs/heads/main"]
    assert repo.git("rev-parse", "HEAD") == head_before and repo.git("status", "--porcelain") == ""
    assert _no_worktrees_left(repo)
    assert "WARNING" not in r.stdout                               # no workflow files involved


def test_emergency_revert_execute_pushes_exact_pre_merge_tree_and_opens_pr(repo, tmp_path):
    base, merge = repo.merged_feature()
    r, calls = _run(REVERT, repo.work, merge, "--execute", "--repo", R, env=repo.env, fixture=PR_OK, tmp=tmp_path)
    assert r.returncode == 0, r.stderr
    [branch] = [b for b in repo.remote_branches() if b != "refs/heads/main"]
    assert re.fullmatch(rf"refs/heads/emergency-revert/{merge[:7]}-\d{{14}}", branch)
    repo.git("fetch", "-q", "origin", f"{branch}:refs/remotes/rb")
    assert repo.tree("rb") == repo.tree(base)                    # byte-for-byte the pre-merge code
    assert repo.git("rev-parse", "rb^") == merge                 # on top of main: no history rewrite
    assert repo.git("rev-parse", "origin/main") == merge         # main itself untouched
    [post] = _writes(calls)
    assert post["path"] == f"repos/{R}/pulls"
    assert post["body"]["title"] == f"EMERGENCY REVERT: {merge}"
    assert post["body"]["base"] == "main" and post["body"]["head"] == branch.removeprefix("refs/heads/")
    assert f"https://github.com/{R}/pull/21" in r.stdout and "no deploy hook" in r.stdout
    assert _no_worktrees_left(repo)


def test_emergency_revert_resolves_a_merged_pr(repo, tmp_path):
    base, merge = repo.merged_feature()
    fx = {f"GET repos/{R}/pulls/20": {"merged": True, "merge_commit_sha": merge}, **PR_OK}
    r, _ = _run(REVERT, repo.work, "--pr", "20", "--repo", R, env=repo.env, fixture=fx, tmp=tmp_path)
    assert r.returncode == 0 and f"PR #20 merge commit: {merge}" in r.stdout and "IDENTICAL" in r.stdout
    fx = {f"GET repos/{R}/pulls/20": {"merged": False, "merge_commit_sha": None}}
    r, _ = _run(REVERT, repo.work, "--pr", "20", "--repo", R, env=repo.env, fixture=fx, tmp=tmp_path)
    assert r.returncode == 1 and "is not merged" in r.stderr


def test_emergency_revert_squash_commit_keeps_later_commits(repo, tmp_path):
    repo.commit("base", **{"app.txt": "v1\n"})
    squash = repo.commit("feat: auth (#20)", **{"app.txt": "v2\n", "auth.txt": "k\n"})
    repo.commit("later: docs", **{"docs.txt": "d\n"})
    repo.push()
    r, _ = _run(REVERT, repo.work, squash, "--execute", "--repo", R, env=repo.env, fixture=PR_OK, tmp=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "single-parent commit" in r.stdout and "1 later commit(s) kept" in r.stdout
    [branch] = [b for b in repo.remote_branches() if b != "refs/heads/main"]
    repo.git("fetch", "-q", "origin", f"{branch}:refs/remotes/rb")
    files = set(repo.git("ls-tree", "--name-only", "rb").split())
    assert files == {"app.txt", "docs.txt"}
    assert repo.git("show", "rb:app.txt") == "v1"


def test_emergency_revert_conflict_fails_without_pushing(repo, tmp_path):
    _base, merge = repo.merged_feature()
    repo.commit("later edit of the same line", **{"app.txt": "v3\n"})
    repo.push()
    r, calls = _run(REVERT, repo.work, merge, "--execute", "--repo", R, env=repo.env, fixture=PR_OK, tmp=tmp_path)
    assert r.returncode == 1 and "CONFLICT" in r.stderr and "app.txt" in r.stderr
    assert repo.remote_branches() == ["refs/heads/main"] and _writes(calls) == []
    assert _no_worktrees_left(repo)


def test_emergency_revert_works_in_a_shallow_clone(repo, tmp_path):
    base, merge = repo.merged_feature()
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo.origin}", str(shallow), env=repo.env)
    assert _git(shallow, "rev-parse", "--is-shallow-repository", env=repo.env) == "true"
    r, _ = _run(REVERT, shallow, merge, env=repo.env)
    assert r.returncode == 0, r.stderr
    assert "shallow clone" in r.stdout and "merge commit" in r.stdout and f"IDENTICAL to {base[:12]}" in r.stdout


@pytest.mark.parametrize("args, code, message", [
    (("deadbeef",), 64, "is not a commit"),
    ((), 64, "Usage"),
    (("HEAD", "--pr", "3"), 64, "not both"),
    (("--pr", "x"), 64, "must be a number"),
    (("--bogus",), 64, "unknown option"),
])
def test_emergency_revert_usage_errors(repo, args, code, message):
    repo.merged_feature()
    r, _ = _run(REVERT, repo.work, *args, env=repo.env)
    assert r.returncode == code and message in r.stderr


def test_emergency_revert_refuses_commits_not_on_main(repo):
    repo.merged_feature()
    repo.git("checkout", "-q", "-b", "side")
    side = repo.commit("unmerged", **{"x.txt": "x\n"})
    r, _ = _run(REVERT, repo.work, side, env=repo.env)
    assert r.returncode == 64 and "is not on origin/main" in r.stderr


def test_emergency_revert_network_and_api_errors_are_fatal(repo, tmp_path):
    _base, merge = repo.merged_feature()
    r, calls = _run(REVERT, repo.work, merge, "--execute", "--repo", R, env=repo.env, fixture={}, tmp=tmp_path)
    assert r.returncode == 2 and "opening the PR failed" in r.stderr and "/compare/main..." in r.stderr
    repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    r, _ = _run(REVERT, repo.work, merge, env=repo.env)
    assert r.returncode == 2 and "git fetch origin main failed" in r.stderr


# ---------------------------------------------------------------------------
# snapshot-prod
# ---------------------------------------------------------------------------

def _gitignore_snapshots(repo: Repo):
    (repo.work / ".gitignore").write_text(".snapshots/\n", encoding="utf-8")


def test_snapshot_writes_verified_files_with_names_only(repo, tmp_path):
    _gitignore_snapshots(repo)
    (repo.work / "nimna").mkdir()
    sha = repo.commit("code", **{
        "nimna/cfg.py": 'import os\nA = _env("NIMNA_API_KEY")\nB = os.getenv("MODEL_PROVIDER", "mock")\n'
                        'C = os.environ["DB_PATH"]\n',
        ".env.example": "NIMNA_ENV=production\nSECRET_TOKEN=super-secret-value-123\n# COMMENTED=1\n"})
    repo.push()
    repo.commit("local only, not pushed", **{"local.txt": "l\n"})
    r, _ = _run(SNAPSHOT, repo.work, env=repo.env)
    assert r.returncode == 0, r.stderr
    snaps = repo.work / ".snapshots"
    [sha_file] = list(snaps.glob("*.sha"))
    stem = sha_file.name[:-4]
    assert re.fullmatch(r"\d{8}T\d{6}Z", stem)
    assert sha_file.read_text().strip() == sha                      # origin/main, not the local commit
    tarball = snaps / f"{stem}.tar.gz"
    tar = subprocess.run(["bash", "-c", f"gzip -dc '{tarball}' > '{tmp_path}/t.tar' && git get-tar-commit-id < '{tmp_path}/t.tar'"],
                         capture_output=True, text=True)
    assert tar.stdout.strip() == sha
    check = subprocess.run(["sha256sum", "-c", f"{stem}.tar.gz.sha256"], cwd=snaps, capture_output=True, text=True)
    assert check.returncode == 0
    names_text = (snaps / f"{stem}.env.names").read_text()
    names = [x for x in names_text.splitlines() if not x.startswith("#")]
    assert names == ["DB_PATH", "MODEL_PROVIDER", "NIMNA_API_KEY", "NIMNA_ENV", "SECRET_TOKEN"]
    assert "super-secret-value-123" not in names_text and "production" not in names
    assert repo.git("status", "--porcelain") == ""                   # ignored: never committed


def test_snapshot_explicit_ref_and_usage_errors(repo, tmp_path):
    _gitignore_snapshots(repo)
    first = repo.commit("one", **{".env.example": "X_ONE=1\n"})
    repo.commit("two", **{".env.example": "X_TWO=1\n"})
    r, _ = _run(SNAPSHOT, repo.work, "--ref", first, "--out", str(tmp_path / "out"), env=repo.env)
    assert r.returncode == 0, r.stderr
    assert next((tmp_path / "out").glob("*.sha")).read_text().strip() == first
    r, _ = _run(SNAPSHOT, repo.work, "--ref", "nope", env=repo.env)
    assert r.returncode == 64 and "is not a commit" in r.stderr


def test_snapshot_refuses_unignored_dir_inside_repo(repo):
    repo.commit("base", **{".env.example": "X=1\n"})
    repo.push()
    r, _ = _run(SNAPSHOT, repo.work, env=repo.env)
    assert r.returncode == 64 and "not git-ignored" in r.stderr
    assert not list((repo.work / ".snapshots").glob("*"))


def test_snapshot_network_error_and_no_partial_files(repo, tmp_path):
    _gitignore_snapshots(repo)
    repo.commit("base", **{".env.example": "X=1\n"})
    repo.push()
    bindir = tmp_path / "badtar"
    bindir.mkdir()
    (bindir / "tar").write_text("#!/bin/sh\nexit 3\n")
    (bindir / "tar").chmod(0o755)
    env = dict(repo.env, PATH=f"{bindir}{os.pathsep}{repo.env['PATH']}")
    r, _ = _run(SNAPSHOT, repo.work, env=env)
    assert r.returncode == 1 and "does not extract" in r.stderr
    assert not list((repo.work / ".snapshots").glob("*"))           # nothing half-written
    repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    r, _ = _run(SNAPSHOT, repo.work, env=repo.env)
    assert r.returncode == 2 and "git fetch origin main failed" in r.stderr


def test_repo_ignores_snapshots():
    r = subprocess.run(["git", "check-ignore", "-q", ".snapshots/x.tar.gz"], cwd=REPO)
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# all ops scripts: 3-line header + hygiene
# ---------------------------------------------------------------------------

OPS_SCRIPTS = ["preflight-prod-check.sh", "rollback-prod.sh", "emergency-revert.sh", "snapshot-prod.sh"]


@pytest.mark.parametrize("name", OPS_SCRIPTS)
def test_ops_script_header_and_hygiene(name):
    path = REPO / "scripts" / name
    lines = path.read_text(encoding="utf-8").splitlines()
    assert os.access(path, os.X_OK)
    assert [ln.split(":")[0] for ln in lines[1:4]] == ["# What", "# When", "# On failure"]
    assert all(len(ln) > 30 for ln in lines[1:4])
    assert "non-zero" in lines[3]
    code = [ln for ln in lines if not ln.lstrip().startswith("#")]
    assert "set -Eeuo pipefail" in code
    joined = "\n".join(code)
    assert "|| true" not in joined and "--force-with-lease" not in joined
    assert not re.search(r"git push[^\n]*(\s-f\b|--force)", joined)
    for ln in code:                                   # `|| :` only for post-report housekeeping
        if "|| :" in ln:
            assert "worktree prune" in ln or "user.email" in ln, ln
    r = subprocess.run(["bash", str(path), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 64 and "# What:" in r.stderr


def test_api_security_doc_has_emergency_rollback_section():
    doc = (REPO / "docs/API-SECURITY.md").read_text(encoding="utf-8")
    assert "## Emergency Rollback (manual)" in doc
    section = doc.split("## Emergency Rollback (manual)", 1)[1].split("\n## ", 1)[0]
    for n in range(1, 8):
        assert re.search(rf"^{n}\. ", section, re.M), f"step {n}"
    for needle in ("emergency-revert.sh", "snapshot-prod.sh", "rollback-prod.sh", "fastapi deploy",
                   "git revert -m 1", "last successful deployment stays live"):
        assert needle in section, needle


def _merge_touching_workflows(repo: Repo) -> str:
    repo.commit("base", **{"app.txt": "v1\n"})
    repo.git("checkout", "-q", "-b", "feature")
    (repo.work / ".github" / "workflows").mkdir(parents=True)
    repo.commit("feature + ci", **{"app.txt": "v2\n", ".github/workflows/ci.yml": "on: push\n"})
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #20")
    repo.push()
    return repo.git("rev-parse", "HEAD")


def test_emergency_revert_warns_in_dry_run_when_workflows_change(repo):
    merge = _merge_touching_workflows(repo)
    r, _ = _run(REVERT, repo.work, merge, env=repo.env)
    assert r.returncode == 0, r.stderr
    assert "WARNING:" in r.stdout and ".github/workflows/ci.yml" in r.stdout and "workflow` scope" in r.stdout


def test_emergency_revert_explains_a_github_workflow_push_rejection(repo, tmp_path):
    merge = _merge_touching_workflows(repo)
    hook = repo.origin / "hooks" / "pre-receive"          # emulate GitHub's refusal for tokens without `workflow`
    hook.write_text("#!/bin/sh\necho 'refusing to allow an OAuth App to create or update workflow "
                    "`.github/workflows/ci.yml` without `workflow` scope' >&2\nexit 1\n")
    hook.chmod(0o755)
    r, calls = _run(REVERT, repo.work, merge, "--execute", "--repo", R, env=repo.env, fixture=PR_OK, tmp=tmp_path)
    assert r.returncode == 2
    assert "GitHub refused the workflow-file change" in r.stderr and "gh auth refresh -s workflow" in r.stderr
    assert _writes(calls) == [] and repo.remote_branches() == ["refs/heads/main"]
