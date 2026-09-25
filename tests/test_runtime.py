"""AgentRuntime: one truth, thin façade, deterministic parity digest."""
import pytest

from nimna.core.state import RunStatus
from nimna.events import (
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    RUNTIME_SHUTDOWN,
    RUN_FINISHED,
    RUN_SUSPENDED,
    SKILLS_USED,
    TOOLS_CALLED,
    EventBus,
    RuntimeEvent,
)
from nimna.providers.base import ModelResponse, ToolCall
from nimna.runtime import AgentRuntime, RunRequest


@pytest.fixture
def runtime(agent, settings):
    rt = AgentRuntime(settings, agent=agent)
    yield rt
    rt.shutdown()


@pytest.fixture
def collector():
    events: list[RuntimeEvent] = []
    return events


def test_boot_builds_live_runtime(settings):
    rt = AgentRuntime.boot(settings)
    try:
        assert rt.settings is settings
        assert rt.agent.settings is settings
        # single truth by reference — nothing is copied
        assert rt.memory is rt.agent.memory
        assert rt.skills is rt.agent.skills
        assert rt.tools is rt.agent.tools
        assert rt.provider is rt.agent.provider
        assert isinstance(rt.bus, EventBus)
        assert rt.is_shutdown is False
    finally:
        rt.shutdown()


def test_run_simple_reply_and_stable_digest(runtime, provider):
    provider.queue('{"skills": []}', "hello back")
    out = runtime.run(RunRequest("hello", session_id="t1"))
    assert out.status == RunStatus.DONE
    assert out.result.reply == "hello back"
    assert len(out.audit_digest) == 64
    int(out.audit_digest, 16)  # valid hex
    assert runtime.audit_digest(out.run_id) == out.audit_digest  # recompute-stable


def test_run_accepts_plain_string(runtime, provider):
    provider.queue('{"skills": []}', "hi")
    out = runtime.run("hi")
    assert out.status == RunStatus.DONE
    assert out.result.session_id  # agent assigns one


def test_run_emits_terminal_events(runtime, provider, collector):
    runtime.bus.subscribe(collector.append)
    provider.queue('{"skills": []}', "done")
    out = runtime.run(RunRequest("go", session_id="t1"))
    types = [e.type for e in collector]
    assert types == [RUN_FINISHED, SKILLS_USED, TOOLS_CALLED]
    assert all(e.run_id == out.run_id and e.session_id == "t1" for e in collector)


def test_one_runtime_one_truth(runtime, provider):
    """Same request, same runtime, two sessions (CLI vs REST) → same truth."""
    provider.queue('{"skills": []}', "hello back")
    provider.queue('{"skills": []}', "hello back")
    a = runtime.run(RunRequest("hello", session_id="t1"))
    b = runtime.run(RunRequest("hello", session_id="t2"))
    assert a.result.skills_used == b.result.skills_used
    assert [c.name for c in a.result.tool_calls] == [c.name for c in b.result.tool_calls]
    assert a.status == b.status == RunStatus.DONE
    assert a.audit_digest == b.audit_digest


def test_suspend_emits_approval_requested(runtime, provider, workspace, collector):
    runtime.bus.subscribe(collector.append)
    runtime.agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    (workspace / "old.txt").write_text("bye", encoding="utf-8")
    provider.queue(
        '{"skills": ["file_analysis"], "reason": "files"}',
        ModelResponse(text="", tool_calls=[
            ToolCall(name="delete_file", arguments={"path": "old.txt"}),
        ]),
        "deleted",
    )
    out = runtime.run(RunRequest("delete old", session_id="s"))
    assert out.status == RunStatus.AWAITING_APPROVAL
    assert out.result.pending is not None
    assert out.result.pending.approval_id == out.run_id
    types = [e.type for e in collector]
    assert RUN_SUSPENDED in types and APPROVAL_REQUESTED in types
    req = next(e for e in collector if e.type == APPROVAL_REQUESTED)
    assert req.payload["approval_id"] == out.run_id
    assert req.payload["tool"] == "delete_file"


def test_approve_continues_suspended_run(runtime, provider, workspace, collector):
    runtime.bus.subscribe(collector.append)
    runtime.agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    (workspace / "old.txt").write_text("bye", encoding="utf-8")
    provider.queue(
        '{"skills": ["file_analysis"], "reason": "files"}',
        ModelResponse(text="", tool_calls=[
            ToolCall(name="delete_file", arguments={"path": "old.txt"}),
        ]),
        "deleted",
    )
    suspended = runtime.run(RunRequest("delete old", session_id="s"))
    assert suspended.status == RunStatus.AWAITING_APPROVAL
    assert len(runtime.pending_approvals("s")) == 1

    done = runtime.approve(suspended.run_id, "s", approved=True)
    assert done.status == RunStatus.DONE
    assert not (workspace / "old.txt").exists()
    assert APPROVAL_RESOLVED in [e.type for e in collector]
    assert runtime.pending_approvals("s") == []


def test_approve_rejects_unknown_id_and_foreign_session(runtime):
    with pytest.raises(KeyError):
        runtime.approve("no-such-run", "s", approved=True)
    # seed a pending run in session "owner", then approve from "intruder"
    runtime.agent.memory.save_pending("r1", "owner", {"run_id": "r1", "session_id": "owner"})
    with pytest.raises(PermissionError):
        runtime.approve("r1", "intruder", approved=True)


def test_resume_deny_path(runtime, provider, workspace):
    runtime.agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    provider.queue(
        '{"skills": ["file_analysis"], "reason": "files"}',
        ModelResponse(text="", tool_calls=[
            ToolCall(name="delete_file", arguments={"path": "notes.txt"}),
        ]),
        "ok, not deleted",
    )
    suspended = runtime.run(RunRequest("delete notes", session_id="s"))
    assert suspended.status == RunStatus.AWAITING_APPROVAL
    resumed = runtime.resume(suspended.run_id, approved=False)
    assert resumed.status == RunStatus.DONE
    assert (workspace / "notes.txt").exists()  # denied → preserved


def test_shutdown_emits_event_freezes_and_idempotent(runtime, provider, collector):
    runtime.bus.subscribe(collector.append)
    runtime.shutdown()
    assert [e.type for e in collector] == [RUNTIME_SHUTDOWN]
    assert runtime.is_shutdown is True
    runtime.shutdown()  # no-op, no duplicate event
    assert [e.type for e in collector] == [RUNTIME_SHUTDOWN]
    with pytest.raises(RuntimeError):
        runtime.run("too late")


@pytest.mark.skip(reason="unskip when app.py routes through AgentRuntime.run()")
def test_cli_and_rest_share_truth():
    """TARGET (not yet runnable): CLI and REST through ONE runtime → same digest.

    Becomes possible only after app.py/cli.py are thinned to delegate to
    AgentRuntime (step 5). Kept visible so the goal does not rot.
    """
    raise NotImplementedError("adapters do not delegate to AgentRuntime yet")


def test_ws_result_matches_runtime_report(agent, provider):
    """Pre-thinning characterization: WS reports vs RuntimeEvent reports.

    WS today: hello → status(thinking) → result(full AgentResult).
    Runtime bus: run_finished/failed/suspended → skills_used → tools_called.
    Both are post-run reports — this locks their mutual consistency WITHOUT
    hooks. After thinning (WS → Runtime), the same run_id must appear on
    both sides; today the ids deliberately differ (two independent runs).
    """
    from fastapi.testclient import TestClient

    from nimna.api.app import create_app

    provider.queue('{"skills": []}', "hello back")  # consumed by the WS run
    provider.queue('{"skills": []}', "hello back")  # consumed by the Runtime run
    client = TestClient(create_app(agent.settings, agent=agent))
    headers = {"X-Nimna-Key": agent.settings.api_keys[0]}
    with client.websocket_connect("/ws/t1", headers=headers) as ws:
        frames = [ws.receive_json()]  # hello
        ws.send_json({"message": "hello", "session_id": "t1"})
        frames += [ws.receive_json(), ws.receive_json()]  # status + result
    assert [f["type"] for f in frames] == ["hello", "status", "result"]
    ws_result = frames[2]["result"]
    assert ws_result["status"] == "done" and ws_result["reply"] == "hello back"

    bus_events: list[RuntimeEvent] = []
    rt = AgentRuntime(agent.settings, agent=agent, bus=EventBus())
    rt.bus.subscribe(bus_events.append)
    try:
        out = rt.run(RunRequest("hello", session_id="t2"))
        bus_types = [e.type for e in bus_events]
    finally:
        rt.shutdown()
    assert bus_types == [RUN_FINISHED, SKILLS_USED, TOOLS_CALLED]
    # same truth, two reports — except identity (the gap thinning will close)
    assert out.result.reply == ws_result["reply"]
    assert out.result.skills_used == ws_result["skills_used"]
    assert [c.name for c in out.result.tool_calls] == [c["name"] for c in ws_result["tool_calls"]]
    assert out.run_id != ws_result["run_id"]
