from fastapi.testclient import TestClient

from nimna.api.app import create_app
from nimna.providers.base import ModelResponse, ToolCall


def test_api_flow(agent, provider, workspace):
    client = TestClient(create_app(agent.settings, agent=agent), headers={"X-Nimna-Key": agent.settings.api_keys[0]})
    assert client.get("/api/health").json()["provider"] == "mock"
    assert "csv_analysis" in {s["name"] for s in client.get("/api/skills").json()["skills"]}
    assert client.get("/api/skills/csv_analysis").json()["meta"]["name"] == "csv_analysis"
    assert client.get("/api/skills/nope").status_code == 404
    assert any(t["name"] == "run_python" for t in client.get("/api/tools").json()["tools"])
    assert "<html" in client.get("/").text

    provider.queue('{"skills": ["csv_analysis"], "reason": "csv"}',
                   ModelResponse(text="", tool_calls=[ToolCall(name="read_csv", arguments={"path": "sales.csv"})]),
                   "analysis done")
    res = client.post("/api/chat", json={"message": "analyse sales.csv"}).json()
    assert res["status"] == "done" and res["reply"] == "analysis done"
    session = res["session_id"]
    assert res["skills_used"] == ["csv_analysis"]
    msgs = client.get(f"/api/sessions/{session}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert client.get(f"/api/sessions/{session}/audit").json()["events"]

    # approval round trip through HTTP
    agent.skills.get("file_analysis").meta.allowed_tools.append("delete_file")
    provider.queue('{"skills": ["file_analysis"]}',
                   ModelResponse(text="", tool_calls=[ToolCall(name="delete_file", arguments={"path": "notes.txt"})]),
                   "deleted")
    res = client.post("/api/chat", json={"message": "delete notes.txt", "session_id": session}).json()
    assert res["status"] == "awaiting_approval" and res["pending"]["tool_name"] == "delete_file"
    assert client.get(f"/api/approvals?session_id={session}").json()["pending"]
    assert client.post("/api/chat", json={"message": "another", "session_id": session}).status_code == 409
    res = client.post(f"/api/approvals/{res['pending']['approval_id']}", json={"approved": True}).json()
    assert res["status"] == "done" and res["reply"] == "deleted"
    assert not (workspace / "notes.txt").exists()
    assert client.post("/api/approvals/unknown", json={"approved": True}).status_code == 404
