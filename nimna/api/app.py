"""FastAPI backend.

    GET  /                      – minimal chat UI
    GET  /api/health            – provider / model / counts
    GET  /api/skills            – catalog (front matter only)
    GET  /api/skills/{name}     – full skill (instructions + references)
    GET  /api/tools             – tool registry with schemas & risk
    POST /api/chat              – run the agent  {session_id?, message}
    POST /api/approvals/{id}    – resolve a pending approval {approved, always?}
    GET  /api/approvals         – list pending approvals
    GET  /api/sessions/{id}/messages
    GET  /api/sessions/{id}/audit
    GET  /api/memories
    POST /api/skills/reload
"""
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..bootstrap import build_agent
from ..config import Settings
from ..core.agent import Agent
from ..core.approval import DeferToClient
from ..core.state import AgentResult

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)
    session_id: Optional[str] = Field(None, description="Omit to start a new session.")


class ApprovalRequest(BaseModel):
    approved: bool
    always: bool = Field(False, description="Also auto-approve this tool for the rest of the run.")


def create_app(settings: Optional[Settings] = None, agent: Optional[Agent] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    agent = agent or build_agent(settings, approval_policy=DeferToClient())

    app = FastAPI(title="Nimna – reusable-skills agent", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.agent = agent
    app.state.settings = settings

    # -- UI --------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        index_file = STATIC_DIR / "index.html"
        if index_file.is_file():
            return index_file.read_text(encoding="utf-8")
        return "<h1>Nimna agent</h1><p>UI not found. See /docs for the API.</p>"

    # -- meta ------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            **agent.provider.describe(),
            "skills": len(agent.skills),
            "tools": len(agent.tools),
            "verify": settings.verify,
            "sandbox": settings.sandbox_backend,
            "auto_approve": settings.auto_approve,
            "limits": {
                "max_steps": settings.max_steps,
                "max_tool_calls": settings.max_tool_calls,
                "max_runtime_seconds": settings.max_runtime_seconds,
                "max_response_tokens": settings.max_response_tokens,
            },
        }

    @app.get("/api/skills")
    def list_skills() -> dict[str, Any]:
        return {
            "skills": [meta.model_dump() for meta in agent.skills.list()],
            "errors": agent.skills.errors,
        }

    @app.get("/api/skills/{name}")
    def get_skill(name: str) -> dict[str, Any]:
        skill = agent.skills.find(name)
        if skill is None:
            raise HTTPException(404, f"unknown skill '{name}'")
        return skill.model_dump()

    @app.post("/api/skills/reload")
    def reload_skills() -> dict[str, Any]:
        agent.skills.reload()
        return {"skills": agent.skills.names(), "errors": agent.skills.errors}

    @app.get("/api/tools")
    def list_tools() -> dict[str, Any]:
        return {"tools": agent.tools.describe()}

    # -- chat ------------------------------------------------------------
    @app.post("/api/chat", response_model=AgentResult)
    async def chat(request: ChatRequest) -> AgentResult:
        session_id = request.session_id or uuid.uuid4().hex[:12]
        if agent.pending_approvals(session_id):
            raise HTTPException(409, "this session has a pending approval; resolve it first")
        if len(request.message) > settings.max_user_message_chars:
            raise HTTPException(413, f"message too long ({len(request.message)} chars); max {settings.max_user_message_chars}")
        return await run_in_threadpool(agent.run, request.message, session_id)

    @app.get("/api/approvals")
    def list_approvals(session_id: Optional[str] = None) -> dict[str, Any]:
        return {"pending": agent.pending_approvals(session_id)}

    @app.post("/api/approvals/{approval_id}", response_model=AgentResult)
    async def resolve_approval(approval_id: str, request: ApprovalRequest) -> AgentResult:
        try:
            return await run_in_threadpool(agent.resume, approval_id, request.approved, always=request.always)
        except KeyError as exc:
            raise HTTPException(404, str(exc))

    # -- sessions & memory ----------------------------------------------
    @app.get("/api/sessions")
    def list_sessions() -> dict[str, Any]:
        return {"sessions": agent.memory.list_sessions()}

    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(session_id: str, limit: int = 100) -> dict[str, Any]:
        return {"session_id": session_id, "messages": agent.memory.get_messages(session_id, limit=limit)}

    @app.get("/api/sessions/{session_id}/audit")
    def session_audit(session_id: str, limit: int = 200) -> dict[str, Any]:
        return {"session_id": session_id, "events": agent.memory.get_audit(session_id, limit=limit)}

    @app.get("/api/memories")
    def memories(kind: Optional[str] = None, q: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        if q:
            return {"memories": agent.memory.search_memories(q, limit=limit)}
        return {"memories": agent.memory.list_memories(kind=kind, limit=limit)}

    @app.delete("/api/memories/{memory_id}")
    def delete_memory(memory_id: int) -> dict[str, Any]:
        return {"deleted": agent.memory.delete_memory(memory_id)}

    return app


def get_app() -> FastAPI:
    """Factory for ``uvicorn nimna.api.app:get_app --factory``."""
    return create_app()


# `uvicorn nimna.api.app:app` also works (built lazily on first import of the attribute)
def __getattr__(name: str):
    if name == "app":
        global app  # noqa: PLW0603
        app = create_app()
        return app
    raise AttributeError(name)
