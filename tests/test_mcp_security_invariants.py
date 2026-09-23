"""Regression tripwires for the MCP security invariants.

Separate from the Arabic evaluation suite on purpose. ``test_arabic_evaluation``
asks *"does this work when everything is Arabic?"*; this file asks *"can the
guarantees be broken?"* — and each test is written so that removing the
guarantee makes it fail, not merely pass differently.

The invariants, and what breaks each one:

1. **A remote tool is always approval-gated.** A tool registered ``safe`` would
   let the governed handler assert ``explicit_consent=True`` on the user's
   behalf, turning that assertion into a bypass. Tripwire: register one ``safe``
   and require the agent to refuse to run it unapproved.
2. **Every request-scoped notification carries its ``run_id``.** An
   unattributable notification cannot be tied to a user action. Tripwire: a real
   SSE notification must land in the journal with the run that caused it.
3. **Scope, policy, approval and audit each precede execution.** Tripwire: for
   each gate, the server must not have received ``tools/call``.
4. **No CLI or API path builds its own agent.** ``build_agent`` is where the
   gateway is attached; an ``Agent(...)`` built anywhere else has no MCP
   governance at all. Tripwire: a spy on ``build_agent`` must be what the API
   calls, and no transport module may construct ``Agent`` directly.
5. **The integrity gate catches a registration that is not ``confirm``.**
   Tripwire: mutate the source in a copy of the repo and require the gate to
   exit non-zero.

These tests are deliberately independent of the client's own helpers where they
can be: the notification check reads the store, the wire check reads the server,
and the gate check runs the gate script as a subprocess on a mutated copy.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import shutil
import subprocess
import sys

import pytest

from mcp_mock_server import MockMCPServer, arabic_tools, default_tools
from nimna.config import Settings
from nimna.core.agent import Agent
from nimna.core.approval import DeferToClient
from nimna.core.state import RunStatus
from nimna.evidence.journal import EvidenceJournal
from nimna.mcp import MCPGateway, MCPServerConfig
from nimna.mcp.naming import (
    decode_tool_name_element,
    encode_name_pattern,
    encode_tool_name_element,
    namespaced_tool_name,
)
from nimna.mcp.registry import MCP_TAG, attach_gateway, register_mcp_tools
from nimna.memory import MemoryStore
from nimna.providers.base import ModelResponse, ToolCall
from nimna.providers.mock import MockProvider
from nimna.skills import SkillManager
from nimna.tools.base import ToolContext
from nimna.tools import default_registry

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"
GATE = REPO / "scripts" / "verify_capabilities.py"


def _settings(workspace: Path) -> Settings:
    settings = Settings.from_env(env_file=None)
    settings.provider = "mock"
    settings.verify = False
    settings.workspace_dir = workspace
    settings.skills_dir = SKILLS
    settings.db_path = Path(":memory:")
    settings.sandbox_backend = "subprocess"
    return settings


def _agent_with_gateway(workspace, provider, gateway, store):
    tools = default_registry()
    report = register_mcp_tools(tools, gateway)
    assert not report.errors, report.errors
    return Agent(
        provider,
        SkillManager(SKILLS),
        tools,
        store,
        _settings(workspace),
        approval_policy=DeferToClient(),
        workspace=workspace,
        mcp_gateway=gateway,
    )


@pytest.fixture
def mock_server():
    with MockMCPServer(tools=default_tools()) as server:
        yield server


@pytest.fixture
def arabic_mock_server():
    with MockMCPServer(tools=arabic_tools()) as server:
        yield server


# ==========================================================================
# 1. A remote tool is always approval-gated
# ==========================================================================


def test_remote_tool_registration_is_forced_to_confirm(mock_server, tmp_path):
    """``declare_risk="safe"`` must not produce a safe remote tool.

    This is the whole reason the handler may assert ``explicit_consent=True``:
    the agent only reaches the handler after an approval, so the assertion
    restates a fact rather than granting a permission.
    """
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=mock_server.url, require_auth=False, declare_risk="safe")],
        enabled=True,
        allow_private_networks=True,
    )
    with gateway:
        definitions = gateway.list_tools("demo")
        assert definitions, "the server should expose at least one tool"
        tools = default_registry()
        register_mcp_tools(tools, gateway)
        registered = [name for name in tools.names() if name.startswith("mcp__")]
        assert registered
        for name in registered:
            tool = tools.get(name)
            assert tool.risk == "confirm", f"{name} must be confirm regardless of declare_risk"
            assert MCP_TAG in tool.tags


def test_agent_refuses_a_remote_tool_that_is_registered_safe(mock_server, tmp_path):
    """The execution point must gate a remote tool even if its risk says safe.

    Guards the case where a tool's declared risk is wrong — a stale definition, a
    direct registry write, a future transport that forgets to publish
    ``confirm``. The agent decides to execute, so the agent is where the
    invariant has to hold.
    """
    store = MemoryStore(":memory:")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = MockProvider()

    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=mock_server.url, require_auth=False)],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
    )
    with gateway:
        tools = default_registry()
        register_mcp_tools(tools, gateway)
        local_name = namespaced_tool_name("demo", "get_weather")
        # Sabotage: the worst case the invariant defends against.
        tools.get(local_name).risk = "safe"

        agent = Agent(
            provider,
            SkillManager(SKILLS),
            tools,
            store,
            _settings(workspace),
            approval_policy=DeferToClient(),
            workspace=workspace,
            mcp_gateway=gateway,
        )
        provider.queue(
            '{"skills": ["mcp_servers"], "reason": "remote", "plan": ["call"]}',
            ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"location": "Cairo"})]),
            "done.",
        )
        result = agent.run("use the MCP tool", session_id="s")

        assert result.status == RunStatus.AWAITING_APPROVAL
        assert "tools/call" not in mock_server.state.called_methods

        agent.resume(result.run_id, approved=True, always=True)
        assert mock_server.state.called_methods.count("tools/call") == 1


def test_handler_called_directly_cannot_grant_itself_consent(mock_server, tmp_path):
    """A direct handler call with no approval record must fail closed.

    This is the bypass the ``explicit_consent=True`` in the handler could have
    become. The handler now checks the run's approval ledger before passing that
    flag on, so an orchestrator that calls the tool directly — skipping the
    agent loop, which is where scope, policy and approval live — gets a refusal
    instead of a remote call.
    """
    store = MemoryStore(":memory:")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=mock_server.url, require_auth=False)],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
    )
    with gateway:
        tools = default_registry()
        register_mcp_tools(tools, gateway)
        local_name = namespaced_tool_name("demo", "get_weather")
        tool = tools.get(local_name)
        assert tool.risk == "confirm"

        ctx = ToolContext(
            settings=_settings(workspace),
            workspace=workspace,
            session_id="s",
            run_id="bypass-attempt",
            # The gateway is attached, as any orchestrator could do — but no
            # approval was recorded for this call.
            extras=attach_gateway(gateway, approved=set()),
        )
        result, ok, _ = tools.execute(local_name, {"location": "Cairo"}, ctx)

    assert ok is False
    assert "without an approval recorded for this call" in result
    # The point: nothing left the process.
    assert "tools/call" not in mock_server.state.called_methods

    # And the evidence the gate demands cannot be forged through the arguments:
    # a caller cannot pass consent as a tool argument either.
    with gateway:
        ctx2 = ToolContext(
            settings=_settings(workspace),
            workspace=workspace,
            session_id="s",
            run_id="bypass-args",
            extras=attach_gateway(gateway, approved=set()),
        )
        result2, ok2, _ = tools.execute(
            namespaced_tool_name("demo", "get_weather"),
            {"location": "Cairo", "explicit_consent": True},
            ctx2,
        )
    assert ok2 is False
    assert "tools/call" not in mock_server.state.called_methods


def test_agent_marks_the_tool_approved_only_at_execution():
    """The ledger is written in ``_run_tool``, the one funnel for execution.

    Placement matters: if the mark were written where the *decision* is made,
    a deferred call would be pre-approved and the resume path would skip the
    check. Asserted over the source because the property is about control flow.
    """
    source = (REPO / "nimna" / "core" / "agent.py").read_text(encoding="utf-8")
    run_tool = source[source.index("def _run_tool("):]
    assert "APPROVED_KEY" in run_tool.split("def ", 2)[1], (
        "the approval ledger must be written inside _run_tool, not at the call sites"
    )
    # Both execution paths go through that funnel.
    assert source.count("self._run_tool(") == 2


# ==========================================================================
# 2. Notifications carry their run_id
# ==========================================================================


def test_sse_notifications_are_recorded_with_their_run_id(arabic_mock_server, tmp_path):
    """A notification recorded without a run cannot be tied to a user action.

    Written against the store rather than the gateway's own return value: the
    regression this guards against was a call site that forgot to pass the id,
    which no unit-level assertion on the outcome would have caught.
    """
    store = MemoryStore(":memory:")
    arabic_mock_server.state.sse_methods.add("tools/call")
    gateway = MCPGateway(
        [MCPServerConfig(name="demo", url=arabic_mock_server.url, require_auth=False)],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
    )
    run_id = "run-اختبار-1"
    with gateway:
        gateway.call_tool(
            "demo",
            "طقس",
            {"مدينة": "القاهرة"},
            explicit_consent=True,
            session_id="s",
            run_id=run_id,
        )

    rows = [row for row in store.get_audit(session_id="s") if row["event"] == "mcp.notification"]
    assert rows, "the SSE stream must have produced a notification to check"
    unattributed = [row for row in rows if not row["run_id"]]
    assert not unattributed, (
        "an MCP notification was recorded without a run_id, so it cannot be "
        "attributed to the run that caused it"
    )
    assert all(row["run_id"] == run_id for row in rows)

    # Attribution must also survive the governed path a user actually takes.
    provider = MockProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway2 = MCPGateway(
        [MCPServerConfig(name="demo", url=arabic_mock_server.url, require_auth=False)],
        enabled=True,
        allow_private_networks=True,
        evidence=EvidenceJournal(store),
    )
    local_name = namespaced_tool_name("demo", "طقس")
    with gateway2:
        agent = _agent_with_gateway(workspace, provider, gateway2, store)
        provider.queue(
            '{"skills": ["mcp_servers"], "reason": "r", "plan": ["p"]}',
            ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"مدينة": "القاهرة"})]),
            "تم.",
        )
        result = agent.run("استخدم أداة MCP", session_id="s2")
        agent.resume(result.run_id, approved=True, always=True)

    agent_rows = [row for row in store.get_audit("s2") if row["event"] == "mcp.notification"]
    assert agent_rows
    assert all(row["run_id"] == result.run_id for row in agent_rows)


def test_an_unattributed_notification_would_be_detectable(arabic_mock_server):
    """Prove the check above can fail: a row with no run_id is caught by it.

    Without this, the assertion could be vacuous — passing because the store
    never records a ``run_id`` field at all.
    """
    store = MemoryStore(":memory:")
    journal = EvidenceJournal(store)
    journal.record("s", None, "mcp.notification", {"server": "demo"})
    rows = [row for row in store.get_audit(session_id="s") if row["event"] == "mcp.notification"]
    assert rows and rows[0]["run_id"] in (None, ""), (
        "the store must expose run_id, otherwise the attribution test proves nothing"
    )
    journal.record("s", "r1", "mcp.notification", {"server": "demo"})
    attributed = [row for row in store.get_audit(session_id="s") if row["run_id"]]
    assert len(attributed) == 1


# ==========================================================================
# 3. Every call passes scope -> policy -> approval -> audit
# ==========================================================================


def test_scope_policy_and_approval_each_precede_execution(mock_server, tmp_path):
    """For each gate, the server must not have seen ``tools/call``."""
    store = MemoryStore(":memory:")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_name = namespaced_tool_name("demo", "get_weather")

    def fresh_gateway():
        return MCPGateway(
            [MCPServerConfig(name="demo", url=mock_server.url, require_auth=False)],
            enabled=True,
            allow_private_networks=True,
            evidence=EvidenceJournal(store),
        )

    # (a) scope: no skill loaded -> the tool is not in this turn's scope.
    provider = MockProvider()
    gateway = fresh_gateway()
    with gateway:
        agent = _agent_with_gateway(workspace, provider, gateway, store)
        provider.queue(
            '{"skills": [], "reason": "r", "plan": ["p"]}',
            ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"location": "Cairo"})]),
            "blocked.",
        )
        result = agent.run("do it", session_id="scope")
    assert result.tool_calls[0].ok is False
    assert "tools/call" not in mock_server.state.called_methods

    # (b) policy: a deny rule must stop it before approval is even offered.
    from nimna.governance import PolicyEngine

    mock_server.state.reset()
    provider = MockProvider()
    gateway = fresh_gateway()
    with gateway:
        agent = _agent_with_gateway(workspace, provider, gateway, store)
        agent.policy = PolicyEngine(deny_tools={local_name})
        provider.queue(
            '{"skills": ["mcp_servers"], "reason": "r", "plan": ["p"]}',
            ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"location": "Cairo"})]),
            "denied.",
        )
        result = agent.run("do it", session_id="policy")
    assert result.status == RunStatus.DONE
    assert "policy denied" in result.tool_calls[0].result_preview
    assert "tools/call" not in mock_server.state.called_methods
    events = [row["event"] for row in store.get_audit("policy")]
    assert "approval_requested" not in events

    # (c) approval: withheld approval means no request ever leaves.
    mock_server.state.reset()
    provider = MockProvider()
    gateway = fresh_gateway()
    with gateway:
        agent = _agent_with_gateway(workspace, provider, gateway, store)
        provider.queue(
            '{"skills": ["mcp_servers"], "reason": "r", "plan": ["p"]}',
            ModelResponse(text="", tool_calls=[ToolCall(name=local_name, arguments={"location": "Cairo"})]),
            "stopped.",
        )
        result = agent.run("do it", session_id="approval")
        assert result.status == RunStatus.AWAITING_APPROVAL
        assert "tools/call" not in mock_server.state.called_methods
        agent.resume(result.run_id, approved=False)
        assert "tools/call" not in mock_server.state.called_methods

    # (d) audit: the whole run is in one verifiable chain.
    assert store.verify_audit_chain(session_id="approval")["valid"] is True


def test_the_wire_never_carries_the_escaped_local_name(mock_server):
    """The local name is escaped; the request must carry the server's name.

    A double-encode or a decoded-name call would send a name the server never
    advertised — silently the wrong tool, or a 404.
    """
    with MockMCPServer(tools={"طقس": arabic_tools()["طقس"]}) as server:
        gateway = MCPGateway(
            [MCPServerConfig(name="demo", url=server.url, require_auth=False)],
            enabled=True,
            allow_private_networks=True,
        )
        with gateway:
            gateway.list_tools("demo")
            local_name = namespaced_tool_name("demo", "طقس")
            assert local_name.isascii()
            gateway.call_tool("demo", "طقس", {"مدينة": "القاهرة"}, explicit_consent=True)

        sent = server.state.calls[-1]
        assert sent.body["params"]["name"] == "طقس"
        assert sent.body["params"]["name"] != local_name
        raw = sent.headers["mcp-name"]
        payload = raw[len("=?base64?") : -len("?=")]
        assert base64.b64decode(payload).decode("utf-8") == "طقس"


# ==========================================================================
# 4. No CLI or API path builds its own agent
# ==========================================================================


def test_api_routes_through_build_agent(tmp_path, monkeypatch):
    """``create_app`` must obtain its agent from ``build_agent``."""
    import nimna.api.app as api_app

    calls: list[str] = []
    real_build_agent = api_app.build_agent

    def spy(*args, **kwargs):
        calls.append("build_agent")
        return real_build_agent(*args, **kwargs)

    monkeypatch.setattr(api_app, "build_agent", spy)
    settings = _settings(tmp_path)
    app = api_app.create_app(settings)
    assert calls == ["build_agent"]
    assert app.state.agent is not None
    # The API's agent gets the gateway when MCP is enabled, so an API run is
    # governed exactly like a CLI run.
    assert hasattr(app.state.agent, "mcp_gateway")


def test_no_transport_module_constructs_an_agent_directly():
    """``Agent(...)`` outside ``bootstrap`` skips gateway attachment.

    Checked over the source because the failure mode is *absence*: an API or CLI
    module that never mentions MCP still compiles, imports and serves requests.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "nimna").rglob("*.py")):
        if path.name in {"agent.py", "bootstrap.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ("Agent(" in stripped or "Agent (" in stripped) and "= Agent(" in stripped:
                offenders.append(f"{path.relative_to(REPO)}:{line_no}: {stripped}")
    assert not offenders, (
        "these modules build their own Agent instead of calling build_agent, so "
        "they would silently run without MCP governance:\n" + "\n".join(offenders)
    )


def test_cli_uses_build_agent():
    source = (REPO / "nimna" / "cli.py").read_text(encoding="utf-8")
    assert "build_agent(" in source
    # Every agent construction site in the CLI must be one of them.
    assert source.count("= Agent(") == 0


# ==========================================================================
# 5. The integrity gate catches a registration that is not confirm
# ==========================================================================


@pytest.mark.skipif(not GATE.exists(), reason="integrity gate script is not present")
def test_integrity_gate_rejects_a_remote_tool_registered_below_confirm(tmp_path):
    """Mutate the registration in a copy and require the gate to fail.

    Run as a subprocess against a temp copy so the working tree is never
    modified. A gate that cannot fail is not a gate, and this is the one that
    stands between the double-gate argument and a silent bypass.
    """
    copy = tmp_path / "repo"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
    registry = copy / "nimna" / "mcp" / "registry.py"
    source = registry.read_text(encoding="utf-8")
    mutated = source.replace('risk="confirm"', 'risk="safe"', 1)
    assert mutated != source, "the registration no longer states risk=confirm, so this test is stale"
    registry.write_text(mutated, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(copy / "scripts" / "verify_capabilities.py")],
        cwd=copy,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "the integrity gate accepted a remote tool registered as safe:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "confirm" in combined or "safety invariant" in combined


def test_integrity_gate_passes_on_the_unmodified_tree():
    """The same gate must pass as-is, so the mutation above is what failed it."""
    result = subprocess.run(
        [sys.executable, str(GATE)], cwd=REPO, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "integrity PASS" in result.stdout


def test_gateway_publishes_the_schema_the_server_sent(mock_server):
    """The server's ``inputSchema`` reaches the model verbatim.

    A locally invented schema would let the model call a tool with arguments the
    server rejects — the failure would surface as a confusing server error
    instead of a clear local refusal.
    """
    with MockMCPServer(tools={"بحث": arabic_tools()["بحث_متقدم"]}) as server:
        gateway = MCPGateway(
            [MCPServerConfig(name="demo", url=server.url, require_auth=False, allowed_tools=frozenset({"بحث_متقدم"}))],
            enabled=True,
            allow_private_networks=True,
        )
        with gateway:
            definitions = gateway.list_tools("demo")
        definition = definitions[0]
        assert definition.input_schema["properties"]["المنطقة"]["x-mcp-header"] == "Region"
        assert "properties" in definition.input_schema


def test_json_audit_rows_are_parseable_with_arabic_payloads(mock_server):
    """Arabic audit payloads must survive a JSON round trip unchanged."""
    store = MemoryStore(":memory:")
    EvidenceJournal(store).record(
        "s", "r", "mcp.tool_call", {"server": "demo", "arguments": {"مدينة": "القاهرة الجديدة 5"}}
    )
    rows = store.get_audit("s")
    encoded = json.dumps(rows, ensure_ascii=False)
    assert "القاهرة" in encoded
    decoded = json.loads(encoded)
    assert decoded[0]["payload"]["arguments"]["مدينة"] == "القاهرة الجديدة 5"


# ==========================================================================
# Naming invariants: escaping must not let one remote tool become another
# ==========================================================================


def test_local_names_cannot_collide():
    """Distinct remote names must map to distinct local names.

    Two names that both exceed the local budget get truncated; without a
    fingerprint of the *full* remote name they would collapse onto one local
    name. In a registry keyed by name that means one remote tool silently
    replacing another — the model would call one tool and reach a different one.
    """
    shared_prefix = "أداة_الطقس_المتقدمة_للمدن_العربية_الكبرى_والصغرى_"
    names = [f"{shared_prefix}{index}_{suffix}" for index, suffix in enumerate(
        ["القاهرة", "الإسكندرية", "أسوان", "طنطا", "المنصورة", "بورسعيد"]
    )]
    local = [namespaced_tool_name("demo", name) for name in names]

    assert len(set(local)) == len(names), f"local names collided: {local}"
    for name, mapped in zip(names, local):
        assert mapped.isascii(), f"{mapped!r} is not provider-safe"
        assert len(mapped) <= 64, f"{mapped!r} exceeds the local budget"
        assert mapped.startswith("mcp__demo__")
    # Determinism: a scope computed at configuration time must match the
    # registry entry built later, so the mapping cannot depend on call order.
    assert [namespaced_tool_name("demo", name) for name in reversed(names)] == list(
        reversed(local)
    )


def test_a_literal_glob_in_a_tool_name_cannot_widen_a_scope():
    """A ``*`` inside a remote tool name must not become a wildcard.

    Names are escaped on the way in, so a tool the server actually called
    ``weather*`` cannot match a skill pattern aimed at ``weather_forecast`` —
    otherwise a hostile or careless server name would grant itself more scope
    than the skill author wrote.
    """
    literal_star = namespaced_tool_name("demo", "weather*")
    literal_question = namespaced_tool_name("demo", "weather?")
    plain = namespaced_tool_name("demo", "weather_forecast")

    # The escaped local names carry no glob metacharacters of their own.
    for mapped in (literal_star, literal_question, plain):
        assert "*" not in mapped and "?" not in mapped

    narrow_scope = encode_name_pattern("mcp__demo__weather_forecast")
    assert fnmatch.fnmatchcase(plain, narrow_scope)
    assert not fnmatch.fnmatchcase(literal_star, narrow_scope)

    # A deliberate wildcard still works, and matches only what it should.
    wide_scope = encode_name_pattern("mcp__demo__weather*")
    assert fnmatch.fnmatchcase(plain, wide_scope)
    assert fnmatch.fnmatchcase(literal_star, wide_scope)
    assert not fnmatch.fnmatchcase(namespaced_tool_name("demo", "طقس"), wide_scope)


def test_escape_is_self_delimiting_across_the_unicode_range():
    """Fixed-width escapes must round-trip, including astral characters.

    A variable-width escape would need a terminator and could be misread; this
    asserts the width assumption holds at the boundaries of the range.
    """
    samples = ["طقس", "أداة٢٠٢٦", "🌤", "\U0010ffff", "x*y?", "a b", "بحث_متقدم"]
    for sample in samples:
        element = encode_tool_name_element(sample)
        assert element.isascii()
        assert decode_tool_name_element(element) == sample, f"{sample!r} did not round-trip"
