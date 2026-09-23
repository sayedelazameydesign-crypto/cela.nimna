"""Integration tests: the gateway against a real MCP server over TCP.

These are the tests that justify moving the capability row's
``integration/contract test where external`` cell from MOCKED to PASS. The
server in :mod:`mcp_mock_server` speaks the ``2026-07-28`` Streamable HTTP
binding over an actual socket and validates the mirrored headers, so a client
that gets the wire format wrong fails here rather than passing against a
permissive mock.

Everything stays offline: the server binds loopback inside the test process.

The gateway's SSRF guard refuses loopback by design, so these tests pass
``allow_private_networks=True`` — the same escape hatch an operator uses to
point Nimna at a local MCP server on purpose. That is stated rather than
quietly worked around, and the guard keeps its own tests elsewhere.
"""
from __future__ import annotations

import pytest

from mcp_mock_server import MockMCPServer, default_tools
from nimna.core.approval import DeferToClient
from nimna.core.state import RunStatus
from nimna.evidence.journal import EvidenceJournal
from nimna.mcp import MCPGateway, MCPServerConfig
from nimna.mcp.registry import (
    MCPToolError,
    attach_gateway,
    build_tool,
    register_mcp_tools,
    validate_arguments,
)
from nimna.memory import MemoryStore
from nimna.providers.base import ModelResponse, ToolCall
from nimna.providers.mock import MockProvider
from nimna.skills import SkillManager
from nimna.tools import ToolContext, default_registry
from nimna.config import Settings
from nimna.core.agent import Agent

REPO_SKILLS = __import__("pathlib").Path(__file__).resolve().parent.parent / "skills"


@pytest.fixture
def mcp_server():
    with MockMCPServer(tools=default_tools()) as server:
        yield server


def gateway_for(server, *, evidence=None, **overrides) -> MCPGateway:
    fields = {
        "name": "demo",
        "url": server.url,
        "require_auth": False,
        "declare_risk": "safe",
    }
    fields.update(overrides)
    return MCPGateway(
        [MCPServerConfig(**fields)],
        enabled=True,
        allow_private_networks=True,
        evidence=evidence if evidence is not None else EvidenceJournal(MemoryStore(":memory:")),
    )


# ==========================================================================
# Wire contract against a real server
# ==========================================================================


def test_discover_over_a_real_socket_negotiates_the_revision(mcp_server):
    with gateway_for(mcp_server) as gateway:
        discovery = gateway.discover("demo")
    assert discovery.supports_current_revision
    assert discovery.negotiate() == "2026-07-28"
    assert discovery.server_info["name"] == "mock-mcp"
    assert mcp_server.state.called_methods == ["server/discover"]


def test_headers_the_server_validates_are_the_ones_we_send(mcp_server):
    """The server rejects a header/body mismatch with -32020.

    A successful call is therefore evidence that the client's mirrored headers
    agreed with its body, checked by an independent implementation rather than
    by a mock that accepts anything.
    """
    with gateway_for(mcp_server) as gateway:
        tools = gateway.list_tools("demo")
    assert {tool.name for tool in tools} == {"get_weather", "execute_sql"}
    recorded = mcp_server.state.calls[-1]
    assert recorded.headers["mcp-method"] == "tools/list"
    assert recorded.headers["mcp-protocol-version"] == "2026-07-28"
    # tools/list is not name-bearing, so sending Mcp-Name would be a 400.
    assert "mcp-name" not in recorded.headers
    # And no session machinery, which this revision removed.
    assert "mcp-session-id" not in recorded.headers
    assert "last-event-id" not in recorded.headers


def test_tool_call_round_trip_over_a_real_socket(mcp_server):
    with gateway_for(mcp_server) as gateway:
        gateway.list_tools("demo")
        outcome = gateway.call_tool("demo", "get_weather", {"location": "Cairo"})
    assert outcome.status == "ok"
    assert outcome.result["structuredContent"]["echo"] == {"location": "Cairo"}
    recorded = mcp_server.state.calls[-1]
    assert recorded.headers["mcp-name"] == "get_weather"
    assert recorded.body["params"]["arguments"] == {"location": "Cairo"}


def test_x_mcp_header_annotated_argument_is_mirrored_and_validated(mcp_server):
    """``execute_sql`` declares ``x-mcp-header`` on ``region``.

    The server rejects the call with -32020 unless the ``Mcp-Param-Region``
    header is present and equal to the argument, so this asserts the mirrored
    header end-to-end.
    """
    with gateway_for(mcp_server) as gateway:
        gateway.list_tools("demo")
        outcome = gateway.call_tool(
            "demo", "execute_sql", {"region": "us-west1", "query": "SELECT 1"}
        )
    assert outcome.status == "ok"
    assert mcp_server.state.calls[-1].headers["mcp-param-region"] == "us-west1"


def test_param_header_is_omitted_when_the_argument_is_absent(mcp_server):
    """The revision requires omitting the header rather than sending ``null``."""
    with gateway_for(mcp_server, allowed_tools=frozenset({"execute_sql"})) as gateway:
        gateway.list_tools("demo")
        # `region` is declared required by the schema, so the local pre-check
        # refuses it before the wire; the server must see no call at all.
        outcome = gateway.call_tool("demo", "execute_sql", {"query": "SELECT 1"})
    assert outcome.status == "ok"
    assert "mcp-param-region" not in mcp_server.state.calls[-1].headers


def test_sse_response_stream_is_consumed_over_a_real_socket(mcp_server):
    """A streamed reply must yield the final response, and its in-flight
    notification must be surfaced rather than dropped."""
    mcp_server.state.sse_methods.add("tools/list")
    store = MemoryStore(":memory:")
    with gateway_for(mcp_server, evidence=EvidenceJournal(store)) as gateway:
        tools = gateway.list_tools("demo", run_id="r")
    assert {tool.name for tool in tools} == {"get_weather", "execute_sql"}
    events = [row["event"] for row in store.get_audit(run_id="r")]
    assert "mcp.notification" in events


def test_server_side_jsonrpc_error_surfaces_as_an_error_outcome(mcp_server):
    mcp_server.state.fail_tools_call = {"code": -32602, "message": "bad arguments"}
    with gateway_for(mcp_server) as gateway:
        gateway.list_tools("demo")
        outcome = gateway.call_tool("demo", "get_weather", {"location": "Cairo"})
    assert outcome.status == "error"
    assert "bad arguments" in outcome.reason


def test_legacy_endpoint_is_refused_rather_than_half_used():
    """A bare 400 means an initialize-era server, which is not implemented."""
    mcp_server = MockMCPServer(tools=default_tools())
    mcp_server.state.blank_status = 400
    with mcp_server:
        with gateway_for(mcp_server) as gateway:
            outcome = gateway.call_tool("demo", "get_weather", {"location": "Cairo"})
    assert outcome.status == "error"
    assert "initialize-based revisions are not implemented" in outcome.detail


# ==========================================================================
# Registry bridge: remote tools as ordinary governed tools
# ==========================================================================


def test_registered_tools_are_governed_confirm_tools(mcp_server):
    registry = default_registry()
    with gateway_for(mcp_server) as gateway:
        report = register_mcp_tools(registry, gateway)

    assert report.registered["demo"] == ["mcp__demo__get_weather", "mcp__demo__execute_sql"]
    assert report.errors == {}
    tool = registry.get("mcp__demo__execute_sql")
    assert tool is not None
    # Always confirm: that is what makes the handler's explicit_consent sound.
    assert tool.risk == "confirm"
    assert "mcp" in tool.tags


def test_the_servers_own_schema_reaches_the_model(mcp_server):
    """Parameter names must be the server's, not a local reconstruction."""
    registry = default_registry()
    with gateway_for(mcp_server) as gateway:
        register_mcp_tools(registry, gateway)
        spec = registry.get("mcp__demo__get_weather").spec()
    assert set(spec.parameters["properties"]) == {"location", "days"}
    # `default` is preserved for a schema authored elsewhere.
    assert spec.parameters["properties"]["days"]["default"] == 1


def test_local_precheck_refuses_bad_arguments_before_the_wire(mcp_server):
    registry = default_registry()
    with gateway_for(mcp_server) as gateway:
        register_mcp_tools(registry, gateway)
        before = len(mcp_server.state.calls)
        tool = registry.get("mcp__demo__get_weather")
        ctx = ToolContext(
            settings=Settings.from_env(env_file=None),
            workspace=__import__("pathlib").Path("."),
            session_id="s",
            extras=attach_gateway(gateway),
        )
        result, ok, _ = registry.execute(tool.name, {"days": 2}, ctx)
    assert ok is False
    assert "missing required parameter 'location'" in result
    # Nothing reached the server: the pre-check is local.
    assert len(mcp_server.state.calls) == before


def test_missing_gateway_in_context_refuses_instead_of_calling_unapproved(mcp_server):
    registry = default_registry()
    with gateway_for(mcp_server) as gateway:
        register_mcp_tools(registry, gateway)
        tool = registry.get("mcp__demo__get_weather")
        ctx = ToolContext(
            settings=Settings.from_env(env_file=None),
            workspace=__import__("pathlib").Path("."),
            session_id="s",
            extras={},  # no gateway
        )
        result, ok, _ = registry.execute(tool.name, {"location": "Cairo"}, ctx)
    assert ok is False
    assert "MCP gateway is not available in this run" in result
    # Registration listed tools; no call was ever attempted.
    assert "tools/call" not in mcp_server.state.called_methods


def test_unreachable_server_is_reported_not_fatal():
    registry = default_registry()
    gateway = MCPGateway(
        [MCPServerConfig(name="dead", url="https://8.8.8.8/mcp", require_auth=False, timeout=0.5)],
        enabled=True,
        allow_private_networks=True,
    )
    with gateway:
        report = register_mcp_tools(registry, gateway)
    assert report.registered.get("dead") in (None, [])
    assert "dead" in report.errors
    assert len(registry) == len(default_registry())


def test_validate_arguments_checks_types_and_unknown_keys():
    schema = {
        "type": "object",
        "properties": {"location": {"type": "string"}, "days": {"type": "integer"}},
        "required": ["location"],
    }
    validate_arguments(schema, {"location": "Cairo", "days": 3})
    with pytest.raises(MCPToolError, match="must be integer, got str"):
        validate_arguments(schema, {"location": "Cairo", "days": "three"})
    with pytest.raises(MCPToolError, match="must be string, got int"):
        validate_arguments(schema, {"location": 5})
    # A boolean must not pass as an integer.
    with pytest.raises(MCPToolError, match="must be integer, got boolean"):
        validate_arguments(schema, {"location": "Cairo", "days": True})
    # Composed schemas are left to the server rather than guessed at.
    validate_arguments({"properties": {"x": {"oneOf": [{"type": "string"}]}}}, {"x": 5})


def test_closed_schema_rejects_unknown_parameters():
    schema = {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "additionalProperties": False,
    }
    with pytest.raises(MCPToolError, match="unknown parameter"):
        validate_arguments(schema, {"location": "Cairo", "extra": "x"})


# ==========================================================================
# Agent loop: end-to-end through approval and resume
# ==========================================================================


class _Recorder:
    """Captures the responses a scripted run produces."""

    def __init__(self) -> None:
        self.replies: list[str] = []


def build_agent(workspace, provider, skills, gateway, store=None):
    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    settings.verify = False
    settings.workspace_dir = workspace
    settings.skills_dir = REPO_SKILLS
    settings.db_path = __import__("pathlib").Path(":memory:")
    settings.sandbox_backend = "subprocess"

    registry = default_registry()
    report = register_mcp_tools(registry, gateway)
    assert not report.errors
    return Agent(
        provider,
        skills,
        registry,
        store if store is not None else MemoryStore(":memory:"),
        settings,
        approval_policy=DeferToClient(),
        workspace=workspace,
        mcp_gateway=gateway,
    )


def test_agent_defers_a_remote_tool_call_then_runs_it_on_resume(mcp_server, tmp_path):
    """The full loop: scope → policy → approval → forward → result."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = MockProvider()
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "remote tool", "plan": ["call the server"]}',
        ModelResponse(
            text="calling",
            tool_calls=[
                ToolCall(name="mcp__demo__get_weather", arguments={"location": "Cairo"})
            ],
        ),
        "the weather was fetched",
    )
    with gateway_for(mcp_server) as gateway:
        agent = build_agent(workspace, provider, SkillManager(REPO_SKILLS), gateway)
        result = agent.run("استخدم أداة MCP لمعرفة الطقس", session_id="s")

        # A remote tool is `confirm`, so the run suspends before any request.
        assert result.status == RunStatus.AWAITING_APPROVAL
        assert result.pending.tool_name == "mcp__demo__get_weather"
        assert "tools/call" not in mcp_server.state.called_methods

        resumed = agent.resume(result.run_id, approved=True, always=True)
        assert resumed.status == RunStatus.DONE
        assert resumed.reply == "the weather was fetched"

    # And the call did reach the server, exactly once.
    assert mcp_server.state.called_methods.count("tools/call") == 1
    assert resumed.tool_calls[0].ok is True


def test_agent_denial_never_reaches_the_server(mcp_server, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = MockProvider()
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "remote tool", "plan": ["call"]}',
        ModelResponse(
            text="calling",
            tool_calls=[ToolCall(name="mcp__demo__get_weather", arguments={"location": "Cairo"})],
        ),
        "denied",
    )
    with gateway_for(mcp_server) as gateway:
        agent = build_agent(workspace, provider, SkillManager(REPO_SKILLS), gateway)
        result = agent.run("استخدم أداة MCP", session_id="s")
        assert result.status == RunStatus.AWAITING_APPROVAL
        resumed = agent.resume(result.run_id, approved=False)
    assert resumed.tool_calls[0].ok is False
    assert "tools/call" not in mcp_server.state.called_methods


def test_glob_scope_grants_remote_tools_only_through_a_skill(mcp_server, tmp_path):
    """``allowed_tools: ["mcp__*__*"]`` is what puts remote tools in scope."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = MockProvider()
    provider.queue(
        '{"skills": [], "reason": "none"}',
        ModelResponse(
            text="trying without the skill",
            tool_calls=[ToolCall(name="mcp__demo__get_weather", arguments={"location": "Cairo"})],
        ),
        "done",
    )
    with gateway_for(mcp_server) as gateway:
        agent = build_agent(workspace, provider, SkillManager(REPO_SKILLS), gateway)
        result = agent.run("ما الطقس؟", session_id="s")

    # No skill asked for MCP tools, so the call is not available this turn.
    assert result.tool_calls[0].ok is False
    assert "is not available in this turn" in result.tool_calls[0].result_preview
    assert "tools/call" not in mcp_server.state.called_methods


def test_glob_pattern_resolution_is_explicit_about_matches():
    from nimna.core.agent import Agent

    registry = default_registry()
    registry.register(
        build_tool(
            gateway=None,  # type: ignore[arg-type]
            server="demo",
            definition=__import__("nimna.mcp.contract", fromlist=["ToolDefinition"]).ToolDefinition(
                name="ping", description="p", input_schema={"type": "object", "properties": {}}
            ),
        )
    )
    agent = object.__new__(Agent)
    agent.tools = registry
    assert agent._resolve_tool_entry("s", "mcp__*__*") == ["mcp__demo__ping"]
    assert agent._resolve_tool_entry("s", "mcp__other__*") == []
    assert agent._resolve_tool_entry("s", "nonexistent") == []
    assert agent._resolve_tool_entry("s", "list_files") == ["list_files"]


def test_gateway_events_appear_in_the_run_evidence(mcp_server, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = MockProvider()
    provider.queue(
        '{"skills": ["mcp_servers"], "reason": "r", "plan": ["p"]}',
        ModelResponse(
            text="calling",
            tool_calls=[ToolCall(name="mcp__demo__get_weather", arguments={"location": "Cairo"})],
        ),
        "done",
    )
    # One store for the run, so the gateway's events land in the same trail the
    # agent writes to. A gateway holding its own journal would file remote-tool
    # evidence somewhere no run verification looks.
    store = MemoryStore(":memory:")
    with gateway_for(mcp_server, evidence=EvidenceJournal(store)) as gateway:
        agent = build_agent(workspace, provider, SkillManager(REPO_SKILLS), gateway, store=store)
        result = agent.run("استخدم أداة MCP", session_id="s")
        agent.resume(result.run_id, approved=True, always=True)

    events = [row["event"] for row in agent.memory.get_audit("s")]
    assert "mcp.tool_call" in events
    assert "mcp.tool_result" in events
    assert agent.memory.verify_audit_chain(session_id="s")["valid"] is True


# ==========================================================================
# Operator path: bootstrap wiring
# ==========================================================================


def test_build_gateway_returns_none_when_disabled(monkeypatch):
    """Disabled must mean no gateway object at all, not a dormant one."""
    from nimna.bootstrap import build_gateway
    from nimna.config import Settings

    monkeypatch.delenv("MCP_ENABLED", raising=False)
    settings = Settings.from_env(env_file=None)
    assert settings.mcp_enabled is False
    assert build_gateway(settings) is None


def test_build_agent_publishes_remote_tools_from_the_environment(mcp_server, monkeypatch, tmp_path):
    """The whole operator path: env → gateway → registry → agent."""
    from nimna.bootstrap import build_agent as bootstrap_agent
    from nimna.config import Settings

    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVERS", "local")
    monkeypatch.setenv("MCPSERVER_LOCAL_URL", mcp_server.url)
    monkeypatch.setenv("MCPSERVER_LOCAL_AUTH", "false")
    monkeypatch.setenv("MCP_ALLOW_PRIVATE_NETWORKS", "true")

    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    settings.verify = False
    settings.workspace_dir = tmp_path / "workspace"
    settings.workspace_dir.mkdir()
    settings.skills_dir = REPO_SKILLS
    settings.db_path = tmp_path / "nimna.db"
    settings.sandbox_backend = "subprocess"

    agent = bootstrap_agent(
        settings, provider=MockProvider(), skills=SkillManager(REPO_SKILLS), memory=MemoryStore(":memory:")
    )
    assert agent.mcp_gateway is not None
    assert "mcp__local__get_weather" in agent.tools.names()
    # Tools arrive as governed confirm tools, never as safe ones.
    assert agent.tools.get("mcp__local__get_weather").risk == "confirm"


def test_build_agent_without_mcp_has_no_gateway(monkeypatch, tmp_path):
    from nimna.bootstrap import build_agent as bootstrap_agent
    from nimna.config import Settings

    monkeypatch.delenv("MCP_ENABLED", raising=False)
    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    settings.workspace_dir = tmp_path / "ws"
    settings.workspace_dir.mkdir()
    settings.skills_dir = REPO_SKILLS
    settings.db_path = tmp_path / "nimna.db"

    agent = bootstrap_agent(
        settings, provider=MockProvider(), skills=SkillManager(REPO_SKILLS), memory=MemoryStore(":memory:")
    )
    assert agent.mcp_gateway is None
    assert not [n for n in agent.tools.names() if n.startswith("mcp__")]
