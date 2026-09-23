"""Offline tests for scripts/evaluate_arena.py (the Arena diff evaluation gate)."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "evaluate_arena.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("evaluate_arena", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses + postponed annotations need a registered module
    spec.loader.exec_module(module)
    return module


ev = _load_module()

# ``---``/``+++`` headers plus removed lines that *start* with ``--`` must not be
# mistaken for headers; a modified, an added, a deleted, a renamed and a binary file.
SAMPLE_DIFF = """\
diff --git a/nimna/core/agent.py b/nimna/core/agent.py
index 1111111..2222222 100644
--- a/nimna/core/agent.py
+++ b/nimna/core/agent.py
@@ -1,4 +1,5 @@
 import os
-old_value = 1
--- this removed line starts with dashes
+new_value = 2
+extra = 3
 print(new_value)
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test_ok():
+    assert True
diff --git a/docs/OLD.md b/docs/OLD.md
deleted file mode 100644
index 4444444..0000000
--- a/docs/OLD.md
+++ /dev/null
@@ -1,2 +0,0 @@
-# old
-gone
diff --git a/skills/a/SKILL.md b/skills/b/SKILL.md
similarity index 100%
rename from skills/a/SKILL.md
rename to skills/b/SKILL.md
diff --git a/workspace/logo.png b/workspace/logo.png
new file mode 100644
index 0000000..5555555
Binary files /dev/null and b/workspace/logo.png differ
"""


def _by_path(files):
    return {f.path: f for f in files}


def test_parse_counts_exclude_headers_and_track_status():
    files = _by_path(ev.parse_unified_diff(SAMPLE_DIFF))
    assert set(files) == {
        "nimna/core/agent.py", "tests/test_new.py", "docs/OLD.md", "skills/b/SKILL.md", "workspace/logo.png",
    }
    agent = files["nimna/core/agent.py"]
    assert (agent.added, agent.removed, agent.status) == (2, 2, "modified")
    assert files["tests/test_new.py"].status == "added"
    assert files["tests/test_new.py"].added == 2
    assert files["docs/OLD.md"].status == "deleted"
    assert files["docs/OLD.md"].removed == 2
    renamed = files["skills/b/SKILL.md"]
    assert (renamed.status, renamed.old_path) == ("renamed", "skills/a/SKILL.md")
    assert files["workspace/logo.png"].binary is True

    totals, categories = ev.summarize(list(files.values()))
    assert totals["added"] == 4 and totals["removed"] == 4
    assert totals["files_added"] == 2 and totals["files_deleted"] == 1 and totals["files_renamed"] == 1
    assert {"code", "tests", "docs", "skills", "other"} <= set(categories)


def test_plain_diff_u_without_git_headers_is_parsed():
    diff = "--- a.txt\t2026-09-23 10:00:00\n+++ a.txt\t2026-09-23 10:01:00\n@@ -1 +1,2 @@\n hello\n+world\n"
    files = ev.parse_unified_diff(diff)
    assert len(files) == 1 and files[0].path == "a.txt" and files[0].added == 1


def test_benchmark_is_skipped_without_api_and_never_claims_pass():
    report = ev.build_report(SAMPLE_DIFF, env={}, base_ref="main", head_sha="abc123")
    assert report["benchmark"]["status"] == "SKIPPED"
    markdown = ev.render_markdown(report)
    assert "SKIPPED" in markdown
    assert "PASS" not in markdown.replace("PASS`", "")  # only the explanatory note mentions PASS
    assert "`main`" in markdown and "`abc123`" in markdown


def test_mock_mode_is_labelled_mocked():
    report = ev.build_report(SAMPLE_DIFF, env={"ARENA_EVAL_MODE": "mock"})
    assert report["benchmark"]["status"] == "MOCKED"
    assert report["benchmark"]["mode"] == "mock"


def test_empty_diff_reports_no_changes():
    report = ev.build_report("", env={})
    assert report["metrics"]["files"] == 0
    assert report["benchmark"]["status"] == "SKIPPED"
    assert "No Diff found" in ev.render_markdown(report)


def test_secret_like_addition_is_flagged_without_leaking_the_value():
    leaked = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    diff = (
        "diff --git a/nimna/config.py b/nimna/config.py\n"
        "--- a/nimna/config.py\n+++ b/nimna/config.py\n@@ -1 +1,2 @@\n x = 1\n"
        f'+OPENAI_API_KEY = "{leaked}"\n'
    )
    report = ev.build_report(diff, env={})
    codes = {s["code"]: s for s in report["risk"]["signals"]}
    assert "secret-like-addition" in codes
    assert report["risk"]["level"] == "HIGH"
    assert codes["secret-like-addition"]["files"] == ["nimna/config.py:2 (openai-style-key)"]
    rendered = ev.render_markdown(report) + json.dumps(report, ensure_ascii=False)
    assert leaked not in rendered


def test_placeholders_and_env_lookups_are_not_flagged():
    diff = (
        "diff --git a/.env.example b/.env.example\n--- a/.env.example\n+++ b/.env.example\n@@ -1 +1,3 @@\n"
        " A=1\n+GEMINI_API_KEY=\n+ARENA_API_KEY=your-arena-key-goes-here\n"
        "diff --git a/nimna/config.py b/nimna/config.py\n--- a/nimna/config.py\n+++ b/nimna/config.py\n@@ -1 +1,2 @@\n"
        " import os\n+api_key = os.environ.get(\"OPENAI_API_KEY\", \"\")\n"
        "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n--- a/.github/workflows/x.yml\n"
        "+++ b/.github/workflows/x.yml\n@@ -1 +1,2 @@\n a: b\n+          ARENA_API_KEY: ${{ secrets.ARENA_API_KEY }}\n"
    )
    assert ev.scan_secrets(ev.parse_unified_diff(diff)) == []


def test_tracked_env_file_is_high_risk():
    diff = "diff --git a/.env b/.env\nnew file mode 100644\n--- /dev/null\n+++ b/.env\n@@ -0,0 +1 @@\n+X=1\n"
    report = ev.build_report(diff, env={})
    assert report["risk"]["level"] == "HIGH"
    assert any(s["code"] == "env-file-tracked" for s in report["risk"]["signals"])


def test_sensitive_path_and_code_without_tests_signals():
    diff = (
        "diff --git a/nimna/tools/sandbox.py b/nimna/tools/sandbox.py\n--- a/nimna/tools/sandbox.py\n"
        "+++ b/nimna/tools/sandbox.py\n@@ -1 +1,2 @@\n x = 1\n+y = 2\n"
    )
    report = ev.build_report(diff, env={})
    codes = {s["code"] for s in report["risk"]["signals"]}
    assert {"sensitive-path", "code-without-tests"} <= codes
    assert report["risk"]["level"] == "MEDIUM"

    # touching tests/ clears the code-without-tests signal
    with_tests = diff + (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
        "@@ -1 +1,2 @@\n a\n+b\n"
    )
    codes = {s["code"] for s in ev.build_report(with_tests, env={})["risk"]["signals"]}
    assert "code-without-tests" not in codes
    assert "sensitive-path" in codes


def test_docs_only_change_is_low_risk():
    diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # x\n+more\n"
    report = ev.build_report(diff, env={})
    assert report["risk"]["level"] == "LOW"
    assert report["risk"]["signals"] == []


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_remote_benchmark_uses_bearer_key_and_normalises_verdict(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(json.dumps({"verdict": "passed", "score": 92, "summary": "ok"}).encode())

    monkeypatch.setattr(ev.urllib.request, "urlopen", fake_urlopen)
    env = {"ARENA_API_URL": "https://arena.example/eval", "ARENA_API_KEY": "k" * 20, "ARENA_TIMEOUT": "5"}
    report = ev.build_report(SAMPLE_DIFF, env=env, base_ref="main")
    assert captured["url"] == "https://arena.example/eval"
    assert captured["auth"] == "Bearer " + "k" * 20
    assert captured["timeout"] == 5.0
    assert captured["body"]["metrics"]["added"] == 4 and "diff" in captured["body"]
    assert report["benchmark"] == {"status": "PASS", "score": 92, "mode": "remote", "detail": "ok"}
    assert "✅ **PASS** (Score: 92)" in ev.render_markdown(report)


def test_remote_benchmark_failure_is_reported_as_error_not_pass(monkeypatch):
    def boom(request, timeout):
        raise ev.urllib.error.URLError("down")

    monkeypatch.setattr(ev.urllib.request, "urlopen", boom)
    env = {"ARENA_API_URL": "https://arena.example/eval", "ARENA_API_KEY": "secret-key-value-123"}
    report = ev.build_report(SAMPLE_DIFF, env=env)
    assert report["benchmark"]["status"] == "ERROR"
    assert "secret-key-value-123" not in json.dumps(report)


def test_cli_writes_markdown_and_json_outputs(tmp_path: Path):
    diff_path = tmp_path / "changes.diff"
    diff_path.write_text(SAMPLE_DIFF, encoding="utf-8")
    md_path, json_path = tmp_path / "result.md", tmp_path / "result.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--diff_file", str(diff_path), "--output", str(md_path),
         "--json-output", str(json_path), "--base-ref", "main"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert md_path.read_text(encoding="utf-8").startswith("### 🎯 Arena Evaluation Results")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["metrics"]["files"] == 5
    assert data["benchmark"]["status"] == "SKIPPED"


def test_cli_strict_mode_fails_on_high_risk(tmp_path: Path):
    diff_path = tmp_path / "changes.diff"
    diff_path.write_text("diff --git a/.env b/.env\n--- /dev/null\n+++ b/.env\n@@ -0,0 +1 @@\n+X=1\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--diff_file", str(diff_path), "--strict", "--format", "json"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=60,
    )
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["risk"]["level"] == "HIGH"
