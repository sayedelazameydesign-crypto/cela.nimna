"""Arabic evaluation suite — governance paths exercised with Arabic input.

Scope, stated plainly so the numbers are not over-read: this evaluates the
**governance paths** (scope, policy, approval, audit, failure handling) when the
user, the tools and the audit trail speak Arabic. It is **not** a model-quality
benchmark. Every test runs on the mock provider, offline, so nothing here says
anything about how well a real model reasons in Arabic.

Each test is named for the path it covers, and ``docs/arabic-evaluation.md``
maps every name to its gate (``G19``/``G20``/``G21``/``G22``) and to the
capability-matrix row it supports.

Test function names are ASCII while the *content* is Arabic: Arabic identifiers
are legal Python but they travel badly through terminal encoding, CI logs and
node-id reporting, and a coverage report nobody can grep is not evidence.

Covered paths
-------------
A. أداة محلية آمنة            safe local tool
B. أداة تتطلب موافقة           approval-requiring tool
C. أداة MCP بعيدة              remote MCP tool
D. رفض بسبب النطاق             scope denial
E. رفض بسبب السياسة            policy denial
F. رفض المستخدم                user denial
G. فشل الاتصال / استجابة غير صالحة
H. إشعارات مرتبطة ب run_id
"""
from __future__ import annotations

import base64
import json
import socket
from pathlib import Path

import pytest

from mcp_mock_server import MockMCPServer, arabic_tools
from nimna.config import Settings
from nimna.core.agent import Agent
from nimna.core.approval import DeferToClient
from nimna.core.state import RunStatus
from nimna.evidence.journal import EvidenceJournal
from nimna.governance import PolicyEngine
from nimna.mcp import MCPGateway, MCPServerConfig
from nimna.mcp.naming import (
    decode_tool_name_element,
    encode_name_pattern,
    namespaced_tool_name,
    split_namespaced_tool,
)
from nimna.mcp.registry import register_mcp_tools
from nimna.memory import MemoryStore
from nimna.providers.base import ModelResponse, ToolCall
from nimna.skills import SkillManager
from nimna.tools import default_registry

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"

#: Arabic request that routes to the ``csv_analysis`` skill by its triggers.
ARABIC_CSV_REQUEST = "حلّل ملف المبيعات sales.csv وأعطني 3 إحصاءات"
#: Arabic request that routes to the ``mcp_servers`` skill.
ARABIC_MCP_REQUEST = "استخدم أداة MCP لمعرفة حالة الطقس في القاهرة"


# ==========================================================================
# Harness
# ==========================================================================


@pytest.fixture
def arabic_server():
    """A real MCP server over TCP exposing Arabic-named tools."""
    with MockMCPServer(tools=arabic_tools()) as server:
        yield server


def arabic_gateway(server, *, store=None, **overrides) -> MCPGateway:
    fields = {"name": "demo", "url": server.url, "require_auth": False, "declare_risk": "safe"}
    fields.update(overrides)
    return MCPGateway(
        [MCPServerConfig(**fields)],
        enabled=True,
        # Loopback is refused by the SSRF guard by design; the escape hatch is
        # the same one an operator uses to point Nimna at a local MCP server.
        allow_private_networks=True,
        evidence=EvidenceJournal(store) if store is not None else None,
    )


def build_arabic_agent(workspace, provider, gateway=None, *, store=None, policy=None):
    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    settings.verify = False
    settings.workspace_dir = workspace
    settings.skills_dir = SKILLS
    settings.db_path = Path(":memory:")
    settings.sandbox_backend = "subprocess"

    tools = default_registry()
    if gateway is not None:
        report = register_mcp_tools(tools, gateway)
        assert not report.errors, report.errors

    agent = Agent(
        provider,
        SkillManager(SKILLS),
        tools,
        store if store is not None else MemoryStore(":memory:"),
        settings,
        approval_policy=DeferToClient(),
        workspace=workspace,
        mcp_gateway=gateway,
    )
    if policy is not None:
        agent.policy = policy
    return agent


@pytest.fixture
def arabic_workspace(tmp_path):
    """A workspace whose files carry Arabic names."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "المبيعات.csv").write_text(
        "الفئة,المبلغ\nمبيعات,1200\nخدمات,800\nمبيعات,400\n", encoding="utf-8"
    )
    (workspace / "تقرير_قديم.txt").write_text("نسخة قديمة ٢٠٢٥\n", encoding="utf-8")
    reports = workspace / "reports"
    reports.mkdir()
    (reports / "تقرير_المبيعات.md").write_text("# تقرير المبيعات\n\nنسخة ٢٠٢٥\n", encoding="utf-8")
    return workspace


# ==========================================================================
# A. أداة محلية آمنة
# ==========================================================================


def test_a_arabic_safe_local_tool_runs_without_approval(arabic_workspace, provider):
    """A safe local tool executes directly, with no approval prompt."""
    provider.queue(
        '{"skills": ["file_analysis"], "reason": "عرض الملفات", "plan": ["اعرض الملفات"]}',
        ModelResponse(text="أعرض الملفات", tool_calls=[ToolCall(name="list_files", arguments={})]),
        "هذه هي الملفات المتاحة.",
    )
    agent = build_arabic_agent(arabic_workspace, provider)
    result = agent.run("اعرض لي ملفات المشروع", session_id="جلسة")

    assert result.status == RunStatus.DONE
    assert result.reply == "هذه هي الملفات المتاحة."
    assert result.tool_calls[0].ok is True
    assert result.tool_calls[0].approved in (None, False)
    assert "تقرير_قديم.txt" in result.tool_calls[0].result_preview

    events = [row["event"] for row in agent.memory.get_audit("جلسة")]
    assert "approval_requested" not in events  # a safe tool never asks


def test_a_arabic_filename_survives_the_tool_round_trip(arabic_workspace, provider):
    """Arabic filenames must survive scope, validation, execution and preview."""
    provider.queue(
        '{"skills": ["csv_analysis"], "reason": "تحليل", "plan": ["اقرأ"]}',
        ModelResponse(
            text="",
            tool_calls=[ToolCall(name="read_csv", arguments={"path": "المبيعات.csv", "max_rows": 2})],
        ),
        "تم التحليل.",
    )
    agent = build_arabic_agent(arabic_workspace, provider)
    result = agent.run(ARABIC_CSV_REQUEST, session_id="جلسة")

    assert result.tool_calls[0].ok is True
    assert "المبلغ" in result.tool_calls[0].result_preview


# ==========================================================================
# B. أداة تتطلب موافقة + F. رفض المستخدم
# ==========================================================================


def test_b_arabic_approval_required_tool_defers_then_runs(arabic_workspace, provider):
    """An approval-requiring tool suspends the run, and resumes when approved.

    The tool is ``write_report`` overwriting an existing Arabic-named report,
    which the risk function raises to ``confirm``. The pending request must be
    explainable to an Arabic-speaking user and must name the Arabic file, so the
    approval is informed rather than a blind yes.
    """
    report = arabic_workspace / "reports" / "تقرير_المبيعات.md"
    original = report.read_text(encoding="utf-8")
    provider.queue(
        '{"skills": ["report_writer"], "reason": "تحديث التقرير", "plan": ["أعد الكتابة"]}',
        ModelResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="write_report",
                    arguments={
                        "title": "تقرير المبيعات",
                        "filename": "تقرير_المبيعات.md",
                        "content_markdown": "# تقرير المبيعات ٢٠٢٦\n\n- مبيعات: 1200\n- خدمات: 800\n",
                        "overwrite": True,
                    },
                )
            ],
        ),
        "تم تحديث التقرير.",
    )
    agent = build_arabic_agent(arabic_workspace, provider)
    result = agent.run("حدّث تقرير المبيعات ٢٠٢٥ بنسخة ٢٠٢٦", session_id="جلسة")

    assert result.status == RunStatus.AWAITING_APPROVAL
    assert result.pending.tool_name == "write_report"
    assert result.pending.summary
    assert "تقرير_المبيعات.md" in result.pending.summary
    assert report.read_text(encoding="utf-8") == original  # nothing written yet

    resumed = agent.resume(result.run_id, approved=True)
    assert resumed.status == RunStatus.DONE
    updated = report.read_text(encoding="utf-8")
    assert "٢٠٢٦" in updated and updated != original
    assert resumed.tool_calls[0].ok is True

    requested = [
        row for row in agent.memory.get_audit("جلسة") if row["event"] == "approval_requested"
    ]
    assert len(requested) == 1
    assert requested[0]["payload"]["tool"] == "write_report"


def test_f_arabic_user_denial_blocks_the_tool(arabic_workspace, provider):
    """رفض المستخدم: the tool must not run, and the refusal must be recorded."""
    report = arabic_workspace / "reports" / "تقرير_المبيعات.md"
    original = report.read_text(encoding="utf-8")
    provider.queue(
        '{"skills": ["report_writer"], "reason": "تحديث التقرير", "plan": ["أعد الكتابة"]}',
        ModelResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="write_report",
                    arguments={
                        "title": "تقرير المبيعات",
                        "filename": "تقرير_المبيعات.md",
                        "content_markdown": "# تقرير مزيف\n",
                        "overwrite": True,
                    },
                )
            ],
        ),
        "لم أكتب التقرير بناءً على رفضك.",
    )
    agent = build_arabic_agent(arabic_workspace, provider)
    result = agent.run("استبدل تقرير المبيعات", session_id="جلسة")
    assert result.status == RunStatus.AWAITING_APPROVAL

    resumed = agent.resume(result.run_id, approved=False)
    assert resumed.tool_calls[0].ok is False
    # The report is untouched — that is the whole point of the denial.
    assert report.read_text(encoding="utf-8") == original

    resolved = [
        row for row in agent.memory.get_audit("جلسة") if row["event"] == "approval_resolved"
    ]
    assert len(resolved) == 1
    assert resolved[0]["payload"]["decision"] == "deny"


# ==========================================================================
# C. أداة MCP بعيدة
# ==========================================================================


def test_c_arabic_remote_mcp_tool_full_governed_path(arabic_server, arabic_workspace, provider):
    """رحلة كاملة: النطاق ← السياسة ← الموافقة ← الخادم ← التدقيق."""
    store = MemoryStore(":memory:")
    local_name = namespaced_tool_name("demo", "طقس")
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "أداة خارجية", "plan": ["استدعِ الأداة"]}',
        ModelResponse(
            text="أستدعي الأداة", tool_calls=[ToolCall(name=local_name, arguments={"مدينة": "القاهرة"})]
        ),
        "حالة الطقس في القاهرة متاحة.",
    )
    with arabic_gateway(arabic_server, store=store) as gateway:
        agent = build_arabic_agent(arabic_workspace, provider, gateway, store=store)
        result = agent.run(ARABIC_MCP_REQUEST, session_id="جلسة")

        # Nothing is sent before the user approves.
        assert result.status == RunStatus.AWAITING_APPROVAL
        assert result.pending.tool_name == local_name
        assert "tools/call" not in arabic_server.state.called_methods

        resumed = agent.resume(result.run_id, approved=True, always=True)

    assert resumed.status == RunStatus.DONE
    assert resumed.tool_calls[0].ok is True
    assert arabic_server.state.called_methods.count("tools/call") == 1

    # The wire carried the server's own Arabic name, not the escaped local one.
    sent = arabic_server.state.calls[-1]
    # The Arabic name travels in the revision's `=?base64?...?=` sentinel,
    # decoded here with base64 directly so the check does not lean on the
    # client's own encoder.
    raw_name = sent.headers["mcp-name"]
    assert raw_name.startswith("=?base64?") and raw_name.endswith("?=")
    payload = raw_name[len("=?base64?") : -len("?=")]
    assert base64.b64decode(payload).decode("utf-8") == "طقس"
    assert sent.body["params"]["name"] == "طقس"
    assert sent.body["params"]["arguments"] == {"مدينة": "القاهرة"}

    events = [row["event"] for row in store.get_audit("جلسة")]
    assert {"mcp.tool_call", "mcp.tool_result", "approval_requested", "approval_resolved"} <= set(events)
    assert store.verify_audit_chain(session_id="جلسة")["valid"] is True


def test_c_arabic_tool_name_is_escaped_locally_but_intact_on_the_wire(arabic_server):
    """An Arabic tool name must not be rejected, and must not be sent escaped."""
    with arabic_gateway(arabic_server) as gateway:
        definitions = gateway.list_tools("demo")
    names = sorted(definition.name for definition in definitions)
    # ترويسة_عربية is excluded (invalid x-mcp-header), the other two remain.
    assert names == ["بحث_متقدم", "طقس"]

    local = namespaced_tool_name("demo", "طقس")
    assert local.isascii(), "the model-facing name must be provider-safe ASCII"
    assert len(local) <= 64
    # Round-trips for a name short enough to need no truncation.
    assert decode_tool_name_element(split_namespaced_tool(local)[1]) == "طقس"
    # And an ASCII name is untouched, so nothing else changes behaviour.
    assert namespaced_tool_name("demo", "get_weather") == "mcp__demo__get_weather"


def test_c_invalid_arabic_header_annotation_excludes_only_that_tool(arabic_server):
    """A spec-invalid annotation (Arabic header name) must not reach the model."""
    with arabic_gateway(arabic_server) as gateway:
        gateway.list_tools("demo")
        rejected = gateway.rejected_tools("demo")
    assert "ترويسة_عربية" in rejected
    assert "ترويسة_عربية" not in {d.name for d in gateway._states["demo"].tools.values()}


def test_c_arabic_x_mcp_header_annotation_uses_ascii_header_name(arabic_server):
    """The parameter is Arabic; the header name it maps to is ASCII."""
    with arabic_gateway(arabic_server) as gateway:
        gateway.list_tools("demo")
        outcome = gateway.call_tool(
            "demo",
            "بحث_متقدم",
            {"المنطقة": "us-west1", "الاستعلام": "SELECT 1"},
            explicit_consent=True,
        )
    assert outcome.status == "ok"
    assert arabic_server.state.calls[-1].headers["mcp-param-region"] == "us-west1"


# ==========================================================================
# D. رفض بسبب النطاق
# ==========================================================================


def test_d_arabic_scope_denial_without_an_mcp_skill(arabic_server, arabic_workspace, provider):
    """Remote tools are out of scope unless a skill asks for them."""
    local_name = namespaced_tool_name("demo", "طقس")
    provider.queue(
        '{"skills": [], "reason": "لا مهارة", "plan": ["جرّب"]}',
        ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"مدينة": "القاهرة"})]),
        "لا أستطيع الوصول لهذه الأداة.",
    )
    with arabic_gateway(arabic_server) as gateway:
        agent = build_arabic_agent(arabic_workspace, provider, gateway)
        result = agent.run("ما الطقس في القاهرة؟", session_id="جلسة")

    assert result.tool_calls[0].ok is False
    assert "is not available in this turn" in result.tool_calls[0].result_preview
    assert "tools/call" not in arabic_server.state.called_methods


def test_d_arabic_glob_pattern_matches_an_arabic_tool(arabic_server):
    """``mcp__*__طقس`` must match the escaped Arabic tool name."""
    pattern = encode_name_pattern("mcp__*__طقس")
    local = namespaced_tool_name("demo", "طقس")
    assert pattern.isascii()
    import fnmatch

    assert fnmatch.fnmatchcase(local, pattern)
    # A pattern for a different server must not match.
    assert not fnmatch.fnmatchcase(local, encode_name_pattern("mcp__آخر__*"))


def test_d_scope_denial_at_the_server_level_is_reported(arabic_server):
    """An operator-declared scope excludes a tool before any request."""
    with arabic_gateway(arabic_server, allowed_tools=frozenset({"طقس"})) as gateway:
        gateway.list_tools("demo")
        before = len(arabic_server.state.calls)
        outcome = gateway.call_tool(
            "demo", "بحث_متقدم", {"المنطقة": "x", "الاستعلام": "y"}, explicit_consent=True
        )
    assert outcome.status == "denied"
    assert "outside the configured scope" in outcome.reason
    assert len(arabic_server.state.calls) == before


# ==========================================================================
# E. رفض بسبب السياسة
# ==========================================================================


def test_e_arabic_policy_denial_stops_a_remote_tool(arabic_server, arabic_workspace, provider):
    """A policy denial must stop the call before approval and before the wire."""
    store = MemoryStore(":memory:")
    local_name = namespaced_tool_name("demo", "طقس")
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "أداة", "plan": ["استدعِ"]}',
        ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"مدينة": "القاهرة"})]),
        "مُنعت الأداة بموجب السياسة.",
    )
    policy = PolicyEngine(deny_tools={local_name})
    with arabic_gateway(arabic_server, store=store) as gateway:
        agent = build_arabic_agent(arabic_workspace, provider, gateway, store=store, policy=policy)
        result = agent.run(ARABIC_MCP_REQUEST, session_id="جلسة")

    # Denied outright — never even a pending approval.
    assert result.status == RunStatus.DONE
    assert result.tool_calls[0].ok is False
    assert "policy denied" in result.tool_calls[0].result_preview
    assert "tools/call" not in arabic_server.state.called_methods
    events = [row["event"] for row in store.get_audit("جلسة")]
    assert "approval_requested" not in events


# ==========================================================================
# G. فشل الاتصال أو استجابة MCP غير صالحة
# ==========================================================================


def test_g_connection_failure_is_reported_not_raised(arabic_workspace, provider):
    """A server that is not listening must be reported, never crash the run."""
    # Bound but never listening: the kernel refuses connections, and the port
    # stays reserved for the duration so no other test server can take it.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_url = f"http://127.0.0.1:{probe.getsockname()[1]}/mcp"

    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=dead_url, require_auth=False, timeout=2.0)],
        enabled=True,
        allow_private_networks=True,
    )
    with gateway:
        # Registration cannot list tools from a dead server, and says so.
        report = register_mcp_tools(default_registry(), gateway)
        assert "demo" in report.errors
        assert report.registered.get("demo") in (None, [])

        # A direct call is refused as an outcome, not raised as an exception.
        outcome = gateway.call_tool(
            "demo", "طقس", {"مدينة": "القاهرة"}, explicit_consent=True
        )
        assert outcome.status == "error"
        assert "did not complete the request" in outcome.reason

    # The consequence for the agent: the tool was never published, so it is not
    # silently available to the model as a route that would fail at call time.
    agent = build_arabic_agent(arabic_workspace, provider)
    assert not [name for name in agent.tools.names() if name.startswith("mcp__")]
    probe.close()


def test_server_name_is_ascii_operator_configuration(arabic_server):
    """Operator config stays ASCII; the Arabic content is what servers expose.

    Escaping the server name instead would put gibberish in every scope and log
    line an operator writes by hand, so the trade is: short ASCII identifier for
    configuration, full Unicode support for tool names and parameters.
    """
    with pytest.raises(Exception, match="server name"):
        MCPServerConfig(name="خادم", url=arabic_server.url, require_auth=False)
    # The same server with an ASCII name exposes its Arabic tools fine.
    assert namespaced_tool_name("demo", "طقس").isascii()


@pytest.mark.parametrize(
    "override, label",
    [
        ({"status": 200, "body": "{ليس JSON"}, "body is not JSON"),
        ({"status": 200, "body": '{"jsonrpc":"2.0","id":999,"result":{"resultType":"complete"}}'},
         "response id does not match the request"),
        ({"status": 200, "body": '{"jsonrpc":"2.0","id":1}'},
         "neither result nor error"),
        ({"status": 200, "body": '{"jsonrpc":"2.0","id":1,"result":{"resultType":"غريب"}}'},
         "unknown resultType"),
        ({"status": 200, "content_type": "text/plain", "body": "نص عادي"},
         "unsupported content type"),
        ({"status": 202, "body": ""}, "202 for a request"),
    ],
)
def test_g_invalid_mcp_responses_are_refused(arabic_server, override, label):
    """An invalid response must be a refusal, never a silently empty success."""
    arabic_server.state.override = override
    with arabic_gateway(arabic_server) as gateway:
        outcome = gateway.call_tool(
            "demo", "طقس", {"مدينة": "القاهرة"}, explicit_consent=True
        )
    assert outcome.status == "error", label
    assert outcome.reason or outcome.detail


def test_g_truncated_sse_stream_is_refused_not_retried(arabic_server):
    """A stream that ends without a final response loses the request."""
    arabic_server.state.override = {
        "status": 200,
        "content_type": "text/event-stream",
        "body": 'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n',
    }
    with arabic_gateway(arabic_server) as gateway:
        outcome = gateway.call_tool(
            "demo", "طقس", {"مدينة": "القاهرة"}, explicit_consent=True
        )
    assert outcome.status == "error"
    assert "not resumable" in outcome.detail


# ==========================================================================
# H. إشعارات مرتبطة ب run_id
# ==========================================================================


def test_h_arabic_run_notifications_are_tied_to_the_run_id(arabic_server, arabic_workspace, provider):
    """Every request-scoped notification must be attributable to its run."""
    store = MemoryStore(":memory:")
    arabic_server.state.sse_methods.add("tools/call")
    local_name = namespaced_tool_name("demo", "طقس")
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "أداة", "plan": ["استدعِ"]}',
        ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"مدينة": "القاهرة"})]),
        "تم.",
    )
    with arabic_gateway(arabic_server, store=store) as gateway:
        agent = build_arabic_agent(arabic_workspace, provider, gateway, store=store)
        result = agent.run(ARABIC_MCP_REQUEST, session_id="جلسة")
        run_id = result.run_id
        final = agent.resume(run_id, approved=True, always=True)
        assert final.status == RunStatus.DONE

    rows = [row for row in store.get_audit(session_id="جلسة") if row["event"] == "mcp.notification"]
    assert rows, "the SSE stream should have produced a notification"
    for row in rows:
        assert row["run_id"] == run_id, "a notification with no run_id is unattributable"
        assert row["payload"]["server"] == "demo"


# ==========================================================================
# واقعية عربية: الخلط والأرقام والسجلات
# ==========================================================================


def test_arabic_mixed_script_request_and_arguments(arabic_server, arabic_workspace, provider):
    """Arabic + English + digits in one request, one argument and one run."""
    store = MemoryStore(":memory:")
    local_name = namespaced_tool_name("demo", "طقس")
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "Mixed", "plan": ["call طقس"]}',
        ModelResponse(
            text="calling طقس",
            tool_calls=[ToolCall(name=local_name, arguments={"مدينة": "القاهرة الجديدة 5", "أيام": 3})],
        ),
        "تم جلب توقعات 3 أيام.",
    )
    with arabic_gateway(arabic_server, store=store) as gateway:
        agent = build_arabic_agent(arabic_workspace, provider, gateway, store=store)
        result = agent.run("اعرض طقس New Cairo لمدة 3 أيام من MCP", session_id="جلسة")
        agent.resume(result.run_id, approved=True, always=True)

    sent = arabic_server.state.calls[-1]
    assert sent.body["params"]["arguments"] == {"مدينة": "القاهرة الجديدة 5", "أيام": 3}


def test_arabic_tool_description_and_parameter_names_reach_the_model(arabic_server, provider):
    """The model must see the server's Arabic description and Arabic params."""
    store = MemoryStore(":memory:")
    with arabic_gateway(arabic_server, store=store) as gateway:
        agent = build_arabic_agent(provider=provider, workspace=REPO / "workspace", gateway=gateway)
        spec = agent.tools.get(namespaced_tool_name("demo", "طقس")).spec()

    assert "الطقس" in spec.description
    assert set(spec.parameters["properties"]) == {"مدينة", "أيام"}
    assert spec.parameters["properties"]["مدينة"]["description"] == "اسم المدينة"
    assert spec.parameters["properties"]["أيام"]["default"] == 1


def test_arabic_audit_log_is_readable_and_verifiable(arabic_server, arabic_workspace, provider):
    """The evidence trail must keep Arabic text intact and verify."""
    store = MemoryStore(":memory:")
    local_name = namespaced_tool_name("demo", "طقس")
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "أداة", "plan": ["استدعِ"]}',
        ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"مدينة": "القاهرة"})]),
        "تم.",
    )
    with arabic_gateway(arabic_server, store=store) as gateway:
        agent = build_arabic_agent(arabic_workspace, provider, gateway, store=store)
        result = agent.run(ARABIC_MCP_REQUEST, session_id="جلسة")
        agent.resume(result.run_id, approved=True, always=True)

    verification = store.verify_audit_chain(session_id="جلسة")
    assert verification["valid"] is True

    rows = store.get_audit(session_id="جلسة")
    assert [row["event"] for row in rows][:1] == ["run_started"]
    # The Arabic request is stored verbatim, not mojibake or a byte-escape blob.
    started = next(row for row in rows if row["event"] == "run_started")
    assert started["payload"]["message"] == ARABIC_MCP_REQUEST

    # Arabic arguments are recoverable from the audit payload.
    call = next(row for row in rows if row["event"] == "mcp.tool_call")
    assert call["payload"]["arguments"] == {"مدينة": "القاهرة"}
    assert call["payload"]["server"] == "demo"

    # And the whole trail is plain JSON, so an external reader can parse it.
    assert json.dumps(rows, ensure_ascii=False, default=str)
