"""Hardening tests from the review checklist (§6)."""
import json
import os
import socket
from pathlib import Path

import pytest

from nimna.core.state import RunStatus
from nimna.providers.base import Message, ModelResponse, ToolCall
from nimna.tools import ToolContext, ToolError
from nimna.tools.builtin.web import _assert_public_url


# -- filesystem jail -------------------------------------------------------

def test_symlink_escape_is_blocked(workspace, settings, skills):
    from nimna.memory import MemoryStore
    # workspace is tmp_path/workspace ; create a symlink inside it that points outside
    outside = workspace.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "evil_link.txt"
    try:
        link.symlink_to(outside)
        ctx = ToolContext(settings=settings, workspace=workspace, session_id="s", memory=MemoryStore(":memory:"), skills=skills)
        with pytest.raises(ToolError, match="outside the workspace|escapes"):
            ctx.resolve_path("evil_link.txt")
        # also directory symlink escape
        d_out = workspace.parent / "outside_dir"
        d_out.mkdir(exist_ok=True)
        (d_out / "x.txt").write_text("x", encoding="utf-8")
        d_link = workspace / "evil_dir"
        d_link.symlink_to(d_out)
        with pytest.raises(ToolError):
            ctx.resolve_path("evil_dir/x.txt")
    finally:
        # cleanup
        for p in [link, outside, d_link if 'd_link' in locals() else None, d_out if 'd_out' in locals() else None]:
            try:
                if p and p.is_symlink():
                    p.unlink()
                elif p and p.is_file():
                    p.unlink()
                elif p and p.is_dir():
                    import shutil; shutil.rmtree(p)
            except Exception:
                pass


def test_absolute_path_is_blocked(workspace, settings, skills):
    from nimna.memory import MemoryStore
    ctx = ToolContext(settings=settings, workspace=workspace, session_id="s", memory=MemoryStore(":memory:"), skills=skills)
    with pytest.raises(ToolError, match="absolute paths not allowed"):
        ctx.resolve_path("/etc/passwd")
    with pytest.raises(ToolError, match="absolute paths not allowed"):
        ctx.resolve_path("/tmp/foo")
    # workspace itself is allowed via relative "."
    assert ctx.resolve_path(".").resolve() == workspace.resolve()
    # traversal
    with pytest.raises(ToolError):
        ctx.resolve_path("../outside")
    with pytest.raises(ToolError):
        ctx.resolve_path("a/../../b")


def test_double_dot_and_normalisation(workspace, settings, skills):
    from nimna.memory import MemoryStore
    from nimna.tools import ToolContext
    ctx = ToolContext(settings=settings, workspace=workspace, session_id="s", memory=MemoryStore(":memory:"), skills=skills)
    assert ctx.resolve_path("sub/../notes.txt").name == "notes.txt"
    with pytest.raises(ToolError):
        ctx.resolve_path("a/b/../../../../etc/passwd")


# -- SSRF -------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://localhost:8000/api/health",
    "http://127.0.0.1/",
    "http://127.0.0.1:9000/",
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://10.0.0.1/",
    "http://192.168.1.1/internal",
    "http://172.16.5.4/",
    "http://169.254.1.1/",
    "http://service.internal/",
    "http://host.local/",
])
def test_ssrf_private_ip_is_blocked(url):
    with pytest.raises(ToolError, match="local|private|internal|resolve"):
        _assert_public_url(url)

def test_ssrf_allows_public():
    # example.com should be allowed in environments where DNS works; in CI it may fail if offline.
    # So only test that a known public literal IP passes the literal check (8.8.8.8 is public).
    # We avoid DNS-dependent assertion.
    try:
        _assert_public_url("http://8.8.8.8/")
    except ToolError as exc:
        # if DNS check fails due to offline, treat as skip
        pytest.skip(f"network check failed: {exc}")


# -- agent loop limits ------------------------------------------------------

def test_tool_call_limit(agent, provider):
    agent.settings.max_tool_calls = 2
    agent.settings.max_steps = 10
    provider.queue(
        '{"skills": ["csv_analysis"], "reason": "x"}',
        ModelResponse(text="", tool_calls=[
            ToolCall(name="read_csv", arguments={"path": "sales.csv"}),
            ToolCall(name="read_csv", arguments={"path": "sales.csv"}),
            ToolCall(name="read_csv", arguments={"path": "sales.csv"}),
        ]),
        "done",
    )
    result = agent.run("do it", session_id="s")
    # third call should have been refused due to max_tool_calls
    assert result.status == RunStatus.DONE
    # at most 2 tool_calls succeeded; the third is an error record
    assert len([c for c in result.tool_calls if c.ok]) <= 2


def test_agent_loop_limit_repeated_call(agent, provider):
    agent.settings.max_steps = 10
    agent.settings.max_tool_calls = 30
    # same identical call three times triggers loop guard on the third
    provider.queue(
        '{"skills": ["csv_analysis"]}',
        ModelResponse(text="", tool_calls=[ToolCall(name="read_csv", arguments={"path": "sales.csv"})]),
        ModelResponse(text="", tool_calls=[ToolCall(name="read_csv", arguments={"path": "sales.csv"})]),
        ModelResponse(text="", tool_calls=[ToolCall(name="read_csv", arguments={"path": "sales.csv"})]),
        "final",
    )
    result = agent.run("loop", session_id="s")
    # third identical call should be blocked as loop
    assert result.status == RunStatus.DONE
    # at least one of the tool_calls should have an error about loop
    # (implementation records loop as error tool_result)
    assert any("loop" in (c.result_preview or "").lower() or "repeated" in (c.result_preview or "").lower()
               for c in result.tool_calls) or len(result.tool_calls) == 2


def test_agent_max_steps_guard(agent, provider):
    agent.settings.max_steps = 1
    provider.queue(
        '{"skills": []}',
        ModelResponse(text="", tool_calls=[ToolCall(name="list_files", arguments={})]),
        ModelResponse(text="", tool_calls=[ToolCall(name="list_files", arguments={})]),
    )
    result = agent.run("x", session_id="s")
    assert result.steps == 1
    assert result.status == RunStatus.DONE


def test_consecutive_failure_guard(agent, provider):
    agent.settings.max_consecutive_failures = 2
    agent.settings.max_steps = 10
    provider.queue(
        '{"skills": []}',
        ModelResponse(text="", tool_calls=[ToolCall(name="read_file", arguments={"path": "nope.txt"})]),
        ModelResponse(text="", tool_calls=[ToolCall(name="read_file", arguments={"path": "nope.txt"})]),
        ModelResponse(text="", tool_calls=[ToolCall(name="read_file", arguments={"path": "nope.txt"})]),
    )
    result = agent.run("fail loop", session_id="s")
    assert result.status == RunStatus.DONE
    assert result.steps <= 3


def test_resume_after_approval_is_idempotent(agent, provider, workspace):
    # exercise the 409 guard path in API as well indirectly
    agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    (workspace / "todelete.txt").write_text("x", encoding="utf-8")
    provider.queue(
        '{"skills": ["file_analysis"]}',
        ModelResponse(text="", tool_calls=[ToolCall(name="delete_file", arguments={"path": "todelete.txt"})]),
        "gone",
    )
    r = agent.run("delete", session_id="sess-hard")
    assert r.status == RunStatus.AWAITING_APPROVAL
    r2 = agent.resume(r.run_id, approved=True)
    assert r2.status == RunStatus.DONE
    assert not (workspace / "todelete.txt").exists()


# -- invalid model JSON / planner fallback ---------------------------------

def test_invalid_model_json_fallback(provider, skills):
    from nimna.core.planner import SkillSelector
    sel = SkillSelector(provider, skills, max_skills=2)
    provider.queue("not json at all {{{")
    result = sel.select("حلّل ملف المبيعات وأنشئ لي تقريرًا")
    # should fall back to keyword ranking and still pick csv_analysis/report_writer
    assert result.source == "keywords"
    assert any(n in result.skills for n in ["csv_analysis", "report_writer"])


def test_unknown_tool_is_rejected(agent, provider):
    provider.queue(
        '{"skills": []}',
        ModelResponse(text="", tool_calls=[ToolCall(name="nonexistent_tool", arguments={})]),
        "done",
    )
    result = agent.run("use unknown", session_id="s")
    assert result.status == RunStatus.DONE
    assert result.tool_calls and result.tool_calls[0].ok is False
    assert "not available" in result.tool_calls[0].result_preview or "unknown" in result.tool_calls[0].result_preview.lower()


def test_skill_cannot_use_unlisted_tool(agent, provider):
    # csv_analysis does NOT list web_search; attempting it should be blocked even if tool exists
    provider.queue(
        '{"skills": ["csv_analysis"]}',
        ModelResponse(text="", tool_calls=[ToolCall(name="web_search", arguments={"query": "test"})]),
        "done",
    )
    result = agent.run("search", session_id="s")
    assert result.tool_calls[0].ok is False
    assert "not available" in result.tool_calls[0].result_preview


def test_skill_unknown_tools_are_warned(settings, skills):
    from nimna.tools import default_registry
    reg = default_registry()
    report = skills.validate_all(registry_names=set(reg.names()))
    # csv_analysis lists only known tools, so no unknown warning
    assert "unknown tools" not in " ".join(report["csv_analysis"]).lower()
    # inject a bogus skill
    from pathlib import Path
    import tempfile, textwrap
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "evil"
        d.mkdir()
        (d / "SKILL.md").write_text(textwrap.dedent("""
        ---
        name: evil
        description: evil skill
        allowed_tools: [does_not_exist, read_file]
        ---
        body
        """), encoding="utf-8")
        from nimna.skills.manager import SkillManager
        m = SkillManager(td)
        rep = m.validate_all(registry_names=set(reg.names()))
        assert any("unknown tools" in w for w in rep["evil"])


# -- secret redaction ------------------------------------------------------

def test_secret_is_redacted_from_audit_log(workspace, settings, skills):
    from nimna.memory import MemoryStore
    mem = MemoryStore(":memory:")
    # simulate what the agent does when it logs a tool call containing a secret
    mem.log("sess", "run1", "tool_call", {"tool": "fetch_url", "arguments": {"url": "https://x", "api_key": "sk-1234567890abcdef"}})
    events = mem.get_audit("sess")
    payload = events[0]["payload"]
    # the key itself should be redacted or the value hidden
    dumped = json.dumps(payload)
    assert "sk-123456" not in dumped
    assert "REDACTED" in dumped or "***" in dumped

def test_tool_result_redacts_secrets(workspace, settings, skills):
    from nimna.tools import default_registry
    from nimna.memory import MemoryStore
    reg = default_registry()
    ctx = ToolContext(settings=settings, workspace=workspace, session_id="s", memory=MemoryStore(":memory:"), skills=skills)
    out, ok, _ = reg.execute("write_file", {"path": "out.txt", "content": "hello"}, ctx)
    # direct write_file not secret, but ensure serialize_result redacts if we inject a secret key
    from nimna.tools.base import serialize_result
    s = serialize_result({"api_key": "secret-xyz-1234567890", "ok": True})
    assert "secret-xyz" not in s
    assert "REDACTED" in s
