import json

from nimna.core.approval import AutoApprove, CallbackPolicy, ConsolePrompt
from nimna.core.planner import SkillSelector, extract_json
from nimna.core.state import Decision, RunStatus
from nimna.providers.base import ModelResponse, ToolCall


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('Sure! ```json\n{"skills": ["a"]}\n```') == {"skills": ["a"]}
    assert extract_json('prefix {"ok": false, "issues": ["x"]} suffix') == {"ok": False, "issues": ["x"]}
    assert extract_json("no json here") is None


def test_planner_uses_llm_json_then_falls_back_to_keywords(provider, skills):
    selector = SkillSelector(provider, skills, max_skills=2)
    provider.queue('{"skills": ["csv_analysis", "unknown_skill", "report_writer"], "reason": "r", "plan": ["a"]}')
    selection = selector.select("analyse sales.csv and write a report")
    assert selection.skills == ["csv_analysis", "report_writer"] and selection.source == "llm"

    provider.queue("I cannot decide")  # garbage -> keyword fallback
    selection = selector.select("ابحث في الإنترنت عن آخر الأخبار")
    assert selection.source == "keywords" and selection.skills[0] == "web_research"


def test_full_run_loads_skills_and_scopes_tools(agent, provider):
    provider.queue(
        '{"skills": ["csv_analysis"], "reason": "csv", "plan": ["inspect", "stats"]}',
        ModelResponse(text="checking", tool_calls=[ToolCall(name="read_csv", arguments={"path": "sales.csv", "max_rows": 1})]),
        ModelResponse(text="trying forbidden tool", tool_calls=[ToolCall(name="web_search", arguments={"query": "x"})]),
        "final answer",
    )
    result = agent.run("حلّل ملف sales.csv", session_id="s")
    assert result.status == RunStatus.DONE and result.reply == "final answer"
    assert result.skills_used == ["csv_analysis"]
    assert [c.name for c in result.tool_calls] == ["read_csv", "web_search"]
    assert result.tool_calls[0].ok and not result.tool_calls[1].ok  # web_search not allowed by this skill

    system_prompt = provider.calls[1]["messages"][0].content
    assert "### Skill: csv_analysis" in system_prompt
    offered = {t.name for t in provider.calls[1]["tools"]}
    assert "read_csv" in offered and "web_search" not in offered and "load_skill" in offered

    events = [e["event"] for e in agent.memory.get_audit("s")]
    assert {"skills_selected", "skill_loaded", "tool_call", "tool_result", "run_finished"} <= set(events)
    history = agent.memory.get_messages("s")
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_load_skill_tool_widens_allowed_tools(agent, provider):
    provider.queue(
        '{"skills": [], "reason": "none"}',
        ModelResponse(text="", tool_calls=[ToolCall(name="load_skill", arguments={"name": "web_research"})]),
        ModelResponse(text="", tool_calls=[ToolCall(name="fetch_url", arguments={"url": "not-a-url"})]),
        "done",
    )
    result = agent.run("hello", session_id="s")
    assert result.skills_used == ["web_research"]
    assert {t.name for t in provider.calls[2]["tools"]} >= {"web_search", "fetch_url"}
    assert result.tool_calls[1].name == "fetch_url" and result.tool_calls[1].ok is False  # ran (and failed validation), not blocked


def test_approval_defer_and_resume(agent, provider, workspace):
    (workspace / "old.txt").write_text("bye", encoding="utf-8")
    provider.queue(
        '{"skills": ["file_analysis"], "reason": "files"}',
        ModelResponse(text="", tool_calls=[
            ToolCall(name="list_files", arguments={}),
            ToolCall(name="delete_file", arguments={"path": "old.txt"}),
            ToolCall(name="file_info", arguments={"path": "notes.txt"}),
        ]),
        "deleted",
    )
    # delete_file is not in file_analysis' allowed tools -> load it via the registry-level allow-list for the test
    agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    result = agent.run("delete old.txt", session_id="s")
    assert result.status == RunStatus.AWAITING_APPROVAL
    assert result.pending.tool_name == "delete_file"
    assert (workspace / "old.txt").exists()
    assert agent.pending_approvals("s")

    resumed = agent.resume(result.run_id, approved=True)
    assert resumed.status == RunStatus.DONE and resumed.reply == "deleted"
    assert not (workspace / "old.txt").exists()
    assert [c.name for c in resumed.tool_calls] == ["list_files", "delete_file", "file_info"]
    assert resumed.tool_calls[1].approved is True
    assert not agent.pending_approvals("s")
    # tool results were fed back in order
    tool_msgs = [m for m in provider.calls[-1]["messages"] if m.role == "tool"]
    assert [m.name for m in tool_msgs] == ["list_files", "delete_file", "file_info"]


def test_approval_denied(agent, provider, workspace):
    agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    provider.queue(
        '{"skills": ["file_analysis"], "reason": "files"}',
        ModelResponse(text="", tool_calls=[ToolCall(name="delete_file", arguments={"path": "notes.txt"})]),
        "ok, not deleted",
    )
    result = agent.run("delete notes", session_id="s")
    resumed = agent.resume(result.run_id, approved=False)
    assert resumed.status == RunStatus.DONE
    assert (workspace / "notes.txt").exists()
    assert resumed.tool_calls[0].approved is False
    denied_msg = [m for m in provider.calls[-1]["messages"] if m.role == "tool"][0]
    assert json.loads(denied_msg.content)["error"] == "denied"


def test_console_prompt_and_always(agent, provider, workspace):
    answers = iter(["a"])
    agent.approval_policy = ConsolePrompt(input_fn=lambda _: next(answers), output=open("/dev/null", "w"))
    agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    (workspace / "a.txt").write_text("a")
    (workspace / "b.txt").write_text("b")
    provider.queue(
        '{"skills": ["file_analysis"], "reason": "files"}',
        ModelResponse(text="", tool_calls=[
            ToolCall(name="delete_file", arguments={"path": "a.txt"}),
            ToolCall(name="delete_file", arguments={"path": "b.txt"}),  # covered by "always"
        ]),
        "done",
    )
    result = agent.run("delete both", session_id="s")
    assert result.status == RunStatus.DONE
    assert not (workspace / "a.txt").exists() and not (workspace / "b.txt").exists()


def test_auto_approve_policy_and_callback(agent, provider, workspace):
    agent.approval_policy = CallbackPolicy(lambda state, tool, call: Decision.APPROVE)
    agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    provider.queue('{"skills": ["file_analysis"]}',
                   ModelResponse(text="", tool_calls=[ToolCall(name="delete_file", arguments={"path": "notes.txt"})]),
                   "gone")
    assert agent.run("rm", session_id="s").reply == "gone"
    assert isinstance(AutoApprove().decide(None, None, None), Decision)


def test_verification_pass_requests_fix(agent, provider):
    agent.settings.verify = True
    provider.queue(
        '{"skills": []}',
        "draft answer",
        '{"ok": false, "issues": ["missing row count"]}',
        "fixed answer",
    )
    result = agent.run("hi", session_id="s")
    assert result.reply == "fixed answer" and result.steps == 2
    review_request = provider.calls[3]["messages"][-1]
    assert review_request.role == "user" and "missing row count" in review_request.content


def test_max_steps_guard(agent, provider):
    agent.settings.max_steps = 2
    provider.queue('{"skills": ["csv_analysis"]}')
    provider.queue(*[ModelResponse(text="again", tool_calls=[ToolCall(name="read_csv", arguments={"path": "sales.csv"})])] * 5)
    result = agent.run("loop", session_id="s")
    assert result.status == RunStatus.DONE and result.steps == 2


def test_history_is_included_in_next_turn(agent, provider):
    provider.queue('{"skills": []}', "first reply", '{"skills": []}', "second reply")
    agent.run("first question", session_id="s")
    agent.run("second question", session_id="s")
    contents = [m.content for m in provider.calls[-1]["messages"]]
    assert "first question" in contents and "first reply" in contents


def test_provider_error_is_reported(agent, provider):
    from nimna.providers.base import ProviderError

    def boom(messages, tools):
        raise ProviderError("quota exceeded")

    provider.queue('{"skills": []}', boom)
    result = agent.run("x", session_id="s")
    assert result.status == RunStatus.ERROR and "quota" in result.error
