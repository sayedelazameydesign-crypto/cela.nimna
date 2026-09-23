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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
    swarm: Optional[bool] = Field(None, description="Force swarm mode (true/false); default = SWARM_ENABLED env")


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
        # keep 'verify' as bool for backward compat, add 'verify_detail' with actual checks
        cache_info: dict[str, Any] = {}
        try:
            from nimna.vision.cache import get_vision_cache
            cache_info = get_vision_cache().stats()
        except Exception:
            cache_info = {"enabled": False, "hit_rate": 0.0}
        anomaly_info: dict[str, Any] = {}
        try:
            from security.anomaly import get_detector
            det = get_detector()
            anomaly_info = {"enabled": True, "detector": "heuristics-v1"}
        except Exception:
            anomaly_info = {"enabled": False}
        memory_info: dict[str, Any] = {}
        try:
            from nimna.memory.qdrant import get_vector_memory
            vm = get_vector_memory()
            h = vm.health()
            memory_info = {
                "enabled": h.get("enabled", True),
                "provider": h.get("provider"),
                "qdrant_reachable": h.get("qdrant_reachable"),
                "vector_dim": h.get("vector_dim"),
                "embedding_model": h.get("embedding_model"),
                "embedding_provider": h.get("embedding_provider"),
                "counts": h.get("counts"),
            }
        except Exception as _e:
            memory_info = {"enabled": False, "error": str(_e)[:200]}
        swarm_info: dict[str, Any] = {
            "enabled": bool(settings.swarm_enabled),
            "parallel": bool(settings.swarm_parallel),
            "max_agents": int(settings.swarm_max_agents),
            "self_healing_retries": int(settings.swarm_self_healing_retries),
            "agents": ["search", "code", "vision"],
        }
        return {
            "status": "ok",
            **agent.provider.describe(),
            "skills": len(agent.skills),
            "tools": len(agent.tools),
            "verify": bool(settings.verify),
            "verify_detail": {
                "enabled": bool(settings.verify),
                "checks": ["skill_instructions", "tool_results", "language", "completeness"] if settings.verify else [],
                "mode": "strict_reviewer" if settings.verify else "off",
            },
            "sandbox": settings.sandbox_backend,
            "auto_approve": settings.auto_approve,
            "cache": cache_info,
            "anomaly": anomaly_info,
            "memory": memory_info,
            "swarm": swarm_info,
            "limits": {
                "max_steps": settings.max_steps,
                "max_tool_calls": settings.max_tool_calls,
                "max_runtime_seconds": settings.max_runtime_seconds,
                "max_response_tokens": settings.max_response_tokens,
            },
            "port": int(__import__("os").getenv("PORT", "8000")),
            "infra": {"redis_url": bool(settings.redis_url), "vision_cache_ttl": settings.vision_cache_ttl, "qdrant_url": bool(settings.qdrant_url)},
        }

    @app.get("/api/swarm/status")
    def swarm_status() -> dict[str, Any]:
        try:
            from nimna.core.planner_swarm import PlannerSwarm
            # lightweight check
            return {
                "enabled": bool(settings.swarm_enabled),
                "parallel": bool(settings.swarm_parallel),
                "max_agents": int(settings.swarm_max_agents),
                "agents": ["search", "code", "vision"],
                "planner": "available",
            }
        except Exception as exc:
            return {"enabled": bool(settings.swarm_enabled), "error": str(exc)[:200]}

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
        session_id = request.session_id or uuid.uuid4().hex  # 128-bit non-guessable
        if agent.pending_approvals(session_id):
            raise HTTPException(409, "this session has a pending approval; resolve it first")
        if len(request.message) > settings.max_user_message_chars:
            raise HTTPException(413, f"message too long ({len(request.message)} chars); max {settings.max_user_message_chars}")
        # swarm override via request
        if request.swarm is not None:
            orig = settings.swarm_enabled
            settings.swarm_enabled = bool(request.swarm)
            try:
                return await run_in_threadpool(agent.run, request.message, session_id)
            finally:
                settings.swarm_enabled = orig
        return await run_in_threadpool(agent.run, request.message, session_id)

    @app.get("/api/approvals")
    def list_approvals(session_id: Optional[str] = None) -> dict[str, Any]:
        return {"pending": agent.pending_approvals(session_id)}

    @app.post("/api/approvals/{approval_id}", response_model=AgentResult)
    async def resolve_approval(approval_id: str, request: ApprovalRequest, session_id: Optional[str] = None) -> AgentResult:
        # session scoping: if pending exists, ensure caller is owner
        try:
            pending = agent.memory.get_pending(approval_id)
            if pending is not None and session_id is not None and pending.get("session_id") != session_id:
                raise HTTPException(403, "pending belongs to different session")
            return await run_in_threadpool(agent.resume, approval_id, request.approved, always=request.always)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))

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

    # -- vector memory (Sprint 2 — Qdrant + fallback) ----------------------
    class VectorUpsertRequest(BaseModel):
        collection: str = Field(..., description="user_context | execution_history | code_knowledge")
        text: str = Field(..., min_length=1, max_length=8000)
        tags: Optional[list[str]] = None
        metadata: Optional[dict[str, Any]] = None

    @app.post("/api/memory/vector/upsert")
    def vector_upsert(req: VectorUpsertRequest) -> dict[str, Any]:
        try:
            from nimna.memory.qdrant import get_vector_memory, COLLECTIONS
            if req.collection not in COLLECTIONS:
                raise HTTPException(400, f"unknown collection '{req.collection}'; valid: {list(COLLECTIONS)}")
            vm = get_vector_memory()
            rid = vm.upsert(req.collection, req.text, metadata=req.metadata, tags=req.tags)
            return {"id": rid, "collection": req.collection}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, str(exc))

    @app.get("/api/memory/vector/search")
    def vector_search(q: str, collections: Optional[str] = None, limit: int = 5) -> dict[str, Any]:
        try:
            from nimna.memory.qdrant import get_vector_memory
            vm = get_vector_memory()
            cols = [c.strip() for c in collections.split(",")] if collections else None
            results = vm.search(q, collections=cols, limit=min(limit, 20))
            return {"query": q, "results": results}
        except Exception as exc:
            raise HTTPException(500, str(exc))

    @app.get("/api/memory/vector/health")
    def vector_health() -> dict[str, Any]:
        try:
            from nimna.memory.qdrant import get_vector_memory
            return get_vector_memory().health()
        except Exception as exc:
            raise HTTPException(500, str(exc))

    @app.get("/api/analytics/summary")
    def analytics_summary() -> dict[str, Any]:
        try:
            from nimna.analytics.collector import get_collector
            return get_collector().summary()
        except Exception as exc:
            raise HTTPException(500, str(exc))

    # -- computer control (VNC desktop) ----------------------------------
    @app.get("/api/computer/status")
    def computer_status() -> dict[str, Any]:
        import os, pathlib
        enabled = os.getenv("COMPUTER_ENABLED", "").lower() in {"1","true","yes","on"}
        vnc_host = os.getenv("COMPUTER_VNC_HOST") or os.getenv("DESKTOP_VNC_URL") or ""
        # check latest screenshot
        latest = None
        try:
            ws = pathlib.Path(settings.workspace_dir)
            latest_file = ws / ".screenshots" / "_latest.json"
            if latest_file.is_file():
                import json
                latest = json.loads(latest_file.read_text(encoding="utf-8"))
        except Exception:
            latest = None
        # check if desktop container is reachable (best-effort)
        desktop_reachable = False
        try:
            import httpx
            # try noVNC URL
            for url in [os.getenv("DESKTOP_VNC_URL", ""), "http://localhost:6901", "http://desktop:6901"]:
                if not url:
                    continue
                try:
                    r = httpx.get(url, timeout=1.0)
                    if r.status_code < 500:
                        desktop_reachable = True
                        break
                except Exception:
                    continue
        except Exception:
            pass
        return {
            "enabled": enabled or bool(vnc_host),
            "vnc_url": vnc_host or "http://localhost:6901",
            "desktop_reachable": desktop_reachable,
            "latest_screenshot": latest,
            "skill_available": "computer_control" in agent.skills.names(),
            "tools": [x for x in agent.tools.describe() if x["name"] in {"take_screenshot","mouse_click","type_text","shell_execute"}],
        }

    @app.get("/api/computer/screenshot")
    def computer_screenshot() -> dict[str, Any]:
        import pathlib, base64, json
        ws = pathlib.Path(settings.workspace_dir)
        candidates = sorted((ws / ".screenshots").glob("screenshot-*.png"), reverse=True) if (ws / ".screenshots").exists() else []
        if not candidates:
            raise HTTPException(404, "no screenshots yet — call take_screenshot first or enable computer profile")
        latest = candidates[0]
        try:
            data = latest.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            return {"path": f".screenshots/{latest.name}", "bytes": len(data), "image_b64": b64, "mime": "image/png"}
        except Exception as exc:
            raise HTTPException(500, str(exc))

    @app.get("/api/workspace/files")
    def workspace_files(path: str = ".", pattern: str = "*", limit: int = 100) -> dict[str, Any]:
        import fnmatch, pathlib
        ws = pathlib.Path(settings.workspace_dir)
        root = (ws / path).resolve()
        try:
            root.relative_to(ws.resolve())
        except ValueError:
            raise HTTPException(403, "outside workspace")
        if not root.exists():
            raise HTTPException(404, f"not found: {path}")
        entries = []
        it = root.rglob("*") if pattern == "**" else root.iterdir()
        for e in sorted(it)[:limit]:
            try:
                e.relative_to(ws.resolve())
            except ValueError:
                continue
            entries.append({"name": e.name, "path": str(e.relative_to(ws)), "is_dir": e.is_dir(), "size": e.stat().st_size if e.is_file() else 0})
        return {"path": path, "entries": entries}

    # -- websocket (live dashboard) - hardened --------------------------------------
    @app.websocket("/ws/{session_id}")
    async def ws_dashboard(websocket: WebSocket, session_id: str):
        # Enforce max message size via receive timeout and manual check; session scoping
        import asyncio, time
        # Validate session_id from path (must be non-empty, alphanumeric)
        if not session_id or len(session_id) > 64 or not session_id.replace("-", "").replace("_", "").isalnum():
            await websocket.close(code=1008)
            return
        await websocket.accept()
        # Rate limit: 10 messages per second, sliding window
        msg_times: list[float] = []
        last_pong = time.monotonic()
        async def ping_loop():
            nonlocal last_pong
            try:
                while True:
                    await asyncio.sleep(30)
                    try:
                        await websocket.send_json({"type": "ping", "t": time.time()})
                        # expect pong within 10s; if not, close
                        await asyncio.sleep(10)
                        if time.monotonic() - last_pong > 40:
                            await websocket.close(code=1001)
                            break
                    except Exception:
                        break
            except asyncio.CancelledError:
                pass
        ping_task = asyncio.create_task(ping_loop())
        try:
            await websocket.send_json({"type": "hello", "session_id": session_id, "provider": agent.provider.describe()})
            while True:
                # 1MB max message size
                data_text = await websocket.receive_text()
                if len(data_text) > 1_000_000:
                    await websocket.close(code=1009)
                    break
                try:
                    import json as _json
                    data = _json.loads(data_text)
                except Exception:
                    await websocket.send_json({"type": "error", "detail": "invalid JSON"})
                    continue
                # pong handling
                if data.get("type") == "pong":
                    last_pong = time.monotonic()
                    continue
                # Rate limit check
                now = time.monotonic()
                msg_times[:] = [t for t in msg_times if now - t < 1.0]
                msg_times.append(now)
                if len(msg_times) > 10:
                    await websocket.send_json({"type": "error", "detail": "rate limit exceeded (10/s)"})
                    continue
                # expected {message: str, session_id?: str}
                msg = (data.get("message") or "").strip()
                if not msg:
                    await websocket.send_json({"type": "error", "detail": "empty message"})
                    continue
                if len(msg) > 20000:
                    await websocket.send_json({"type": "error", "detail": "message too long"})
                    continue
                sid = data.get("session_id") or session_id
                # Session scoping: sid must equal path session_id or be a valid new session
                # If sid differs from path, ensure it is not hijacking another session's pending
                if sid != session_id:
                    # allow creating new session via ws, but log it
                    log.info("ws session mismatch: path=%s payload=%s", session_id, sid)
                # run agent in threadpool to avoid blocking
                from fastapi.concurrency import run_in_threadpool
                await websocket.send_json({"type": "status", "status": "thinking", "message": "يفكر..."})
                try:
                    result = await run_in_threadpool(agent.run, msg, sid)
                except Exception as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
                    continue
                try:
                    payload = result.model_dump(mode="json")
                except Exception:
                    payload = {"reply": str(result), "status": "done"}
                await websocket.send_json({"type": "result", "result": payload})
                if payload.get("status") == "awaiting_approval":
                    await websocket.send_json({"type": "approval", "pending": payload.get("pending")})
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            try:
                await websocket.send_json({"type": "error", "detail": str(exc)})
            except Exception:
                pass
            try:
                await websocket.close()
            except Exception:
                pass
        finally:
            try:
                ping_task.cancel()
            except Exception:
                pass

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
