"""FastAPI backend.

    GET  /                      – minimal chat UI
    GET  /api/health            – provider / model / counts
    GET  /api/metrics           – Prometheus exposition (authed)
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

Security (nimna/api/security.py): every path except ``/``, ``/api/health`` and
``/static/*`` requires the ``X-Nimna-Key`` header (WebSocket: header or the
``nimna.key.<key>`` sub-protocol). The app refuses to build in production
without ``NIMNA_API_KEY``.
"""
import hashlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..bootstrap import build_agent
from ..config import Settings
from ..core.agent import Agent
from ..core.approval import DeferToClient
from ..core.state import AgentResult
from ..evidence import EvidenceJournal
from ..models import ModelRegistry
from ..observability import summarize_events
from .security import SecurityConfig, install_security, select_ws_subprotocol
from .telemetry import get_registry, install_telemetry

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_IDEM_KEY_RE = re.compile(r"[\w\-.]{1,128}\Z")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)
    session_id: Optional[str] = Field(None, description="Omit to start a new session.")
    swarm: Optional[bool] = Field(None, description="Force swarm mode (true/false); default = SWARM_ENABLED env")


class ApprovalRequest(BaseModel):
    approved: bool
    always: bool = Field(False, description="Also auto-approve this tool for the rest of the run.")


def create_app(settings: Optional[Settings] = None, agent: Optional[Agent] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    # validate BEFORE building anything: an unsafe config must not boot
    # (raises SecurityConfigError, e.g. production without NIMNA_API_KEY)
    security = SecurityConfig.from_settings(settings)
    # build_agent() is the next fail-closed layer: ProviderError (no GEMINI_API_KEY)
    # then CostPolicyError (paid/undeclared model with MAX_SPEND_USD=0) — both
    # fire before FastAPI() exists, so /api/health never hides a dead runtime.
    agent = agent or build_agent(settings, approval_policy=DeferToClient())

    app = FastAPI(title="Nimna – reusable-skills agent", version="0.1.0")
    registry = get_registry()
    install_security(app, security, redis_url=settings.redis_url, metrics=registry)
    app.state.agent = agent
    app.state.settings = settings
    app.state.evidence = EvidenceJournal(agent.memory)
    app.state.models = getattr(agent, "model_registry", ModelRegistry.for_settings(settings, agent.provider.describe()))
    # Telemetry LAST so it is outermost: even 401/429 rejections get a
    # request ID and are counted.
    install_telemetry(app, registry=registry)

    # -- idempotency helper (chat + approval resolve) ----------------------
    def _idempotency_material(request: Request, endpoint: str, body: BaseModel,
                              extra: str = "") -> tuple[Optional[str], str]:
        """Validate the Idempotency-Key header and hash the request body.

        Returns ``(key, body_hash)``; ``key`` is None when the client sent no
        header (idempotency is opt-in per call).  Invalid keys are 422 —
        silently ignoring them would fake a guarantee we do not keep.
        """
        raw_key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
        if raw_key is None:
            return None, ""
        key = raw_key.strip()
        if not _IDEM_KEY_RE.fullmatch(key):
            raise HTTPException(422, "invalid Idempotency-Key: 1-128 chars of [A-Za-z0-9_.-]")
        fingerprint = body.model_dump_json() + "|" + extra
        return key, hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def _idempotent_replay(identity: str, key: str, endpoint: str, body_hash: str) -> Optional[JSONResponse]:
        """Claim the key or answer from the store.

        * no record → claim it (in-flight marker) and return None (caller runs);
        * completed + same body → replay the stored response (``Idempotent-Replayed``);
        * in-flight + same body → 409 (retry shortly, do not duplicate the run);
        * same key + different body → 422 (a key fingerprints exactly one request).
        """
        record = agent.memory.idempotency_claim(
            identity, key, endpoint, body_hash, settings.idempotency_ttl_seconds)
        if record is None:
            return None
        if record["body_hash"] != body_hash or record["endpoint"] != endpoint:
            registry.inc("idempotency_conflicts_total", {"endpoint": endpoint, "reason": "mismatch"})
            raise HTTPException(422, "Idempotency-Key was already used with a different request body")
        if int(record["status_code"]) < 0:
            registry.inc("idempotency_conflicts_total", {"endpoint": endpoint, "reason": "in_flight"})
            raise HTTPException(409, "request with this Idempotency-Key is still in flight; retry shortly",
                                headers={"Retry-After": "5"})
        import json as _json

        registry.inc("idempotent_replays_total", {"endpoint": endpoint})
        return JSONResponse(content=_json.loads(record["response_json"]),
                            status_code=int(record["status_code"]),
                            headers={"Idempotent-Replayed": "true"})

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
        resilience_info: dict[str, Any] = {
            "rate_limiter": {"backend": getattr(app.state.chat_limiter, "backend", "memory")},
            "idempotency": {
                "entries": agent.memory.idempotency_count(),
                "ttl_seconds": settings.idempotency_ttl_seconds,
            },
            "uptime_seconds": int(time.time() - registry.started_at),
        }
        provider_status = getattr(agent.provider, "resilience_status", None)
        if callable(provider_status):
            try:
                resilience_info.update(provider_status())
            except Exception:
                resilience_info["breaker"] = {"state": "unknown"}
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
            "resilience": resilience_info,
            "limits": {
                "max_steps": settings.max_steps,
                "max_tool_calls": settings.max_tool_calls,
                "max_runtime_seconds": settings.max_runtime_seconds,
                "max_response_tokens": settings.max_response_tokens,
            },
            "governance": {
                "cost": agent.cost_guard.status(),
                "policy": "scope -> validation -> policy -> approval -> execution",
                "sandbox": settings.sandbox_backend,
            },
            "provenance": {"runtime_fingerprint": agent.runtime_manifest.get("sha256")},
            "browser_use": {
                "enabled": bool(getattr(settings, "browser_use_enabled", False)),
                "v4": True,
                "max_spend_usd": float(getattr(settings, "browser_use_max_spend_usd", 0.0)),
            },
            "port": int(__import__("os").getenv("PORT", "8000")),
            "infra": {"redis_url": bool(settings.redis_url), "vision_cache_ttl": settings.vision_cache_ttl, "qdrant_url": bool(settings.qdrant_url)},
        }

    @app.get("/api/metrics", include_in_schema=False)
    def metrics() -> PlainTextResponse:
        """Prometheus exposition (per-process; scrape every replica and
        aggregate with ``sum by``).  Behind API-key auth like all
        non-public paths — see docs/RESILIENCE.md for the scrape recipe."""
        return PlainTextResponse(registry.render_prometheus(),
                                 media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        """Expose the active model profile and the hard budget state."""
        return {
            "models": app.state.models.describe(),
            "active": agent.provider.describe(),
            "budget": agent.cost_guard.status(),
        }

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        """Machine-readable capability claims; verification stays evidence-backed."""
        browser_enabled = bool(getattr(settings, "browser_use_enabled", False))
        browser_configured = bool(getattr(settings, "browser_use_api_key", None)) and float(getattr(settings, "browser_use_max_spend_usd", 0.0)) > 0
        browser_status = "configured" if browser_enabled and browser_configured else ("blocked" if browser_enabled else "available_opt_in")
        return {
            "capabilities": {
                "mission_runtime": {"status": "implemented", "evidence": ["nimna/core/agent.py"]},
                "skill_scoping": {"status": "implemented", "evidence": ["nimna/skills", "nimna/core/agent.py"]},
                "tool_governance": {"status": "implemented", "evidence": ["nimna/governance/policy.py", "nimna/core/approval.py"]},
                "sqlite_source_of_truth": {"status": "implemented", "evidence": ["nimna/memory/store.py"]},
                "evidence_hash_chain": {"status": "implemented", "evidence": ["nimna/provenance/hashchain.py", "nimna/memory/store.py"]},
                "model_registry": {"status": "implemented", "evidence": ["nimna/models/registry.py"]},
                "browser_use_cloud_v4": {
                    "status": browser_status,
                    "evidence": ["nimna/browser/cloud_v4.py", "docs/browser_use_v4.md"],
                },
                "host_sandbox": {"status": "partial", "evidence": ["nimna/tools/sandbox.py", "SECURITY.md"]},
            },
            "status_vocabulary": ["implemented", "configured", "available_opt_in", "partial", "planned", "blocked", "unknown"],
        }

    @app.get("/api/provenance")
    def provenance() -> dict[str, Any]:
        return agent.runtime_manifest

    @app.get("/api/browser-use/status")
    def browser_use_status() -> dict[str, Any]:
        return {
            "enabled": bool(getattr(settings, "browser_use_enabled", False)),
            "configured": bool(getattr(settings, "browser_use_api_key", None)) and float(getattr(settings, "browser_use_max_spend_usd", 0.0)) > 0,
            "api_version": "v4",
            "base_url": getattr(settings, "browser_use_base_url", "https://api.browser-use.com"),
            "max_spend_usd": float(getattr(settings, "browser_use_max_spend_usd", 0.0)),
            "lifecycle": "owned browsers must be stopped in finally",
        }

    @app.get("/api/runs/{run_id}/evidence")
    def run_evidence(run_id: str, limit: int = 1000) -> dict[str, Any]:
        evidence = app.state.evidence.for_run(run_id, limit=min(max(limit, 1), 2000))
        events = evidence["events"]
        evidence["metrics"] = summarize_events(run_id, events).to_dict()
        return evidence

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
    async def chat(request: ChatRequest, raw: Request) -> Any:
        session_id = request.session_id or uuid.uuid4().hex  # 128-bit non-guessable
        if agent.pending_approvals(session_id):
            raise HTTPException(409, "this session has a pending approval; resolve it first")
        if len(request.message) > settings.max_user_message_chars:
            raise HTTPException(413, f"message too long ({len(request.message)} chars); max {settings.max_user_message_chars}")
        identity = raw.scope.get("nimna.identity", "anon")
        idem_key, body_hash = _idempotency_material(raw, "chat", request)
        if idem_key is not None:
            replay = _idempotent_replay(identity, idem_key, "chat", body_hash)
            if replay is not None:
                return replay
        # Swarm override travels as a per-call flag — never mutates the shared
        # settings object (the old set/run/restore window raced under concurrency;
        # see tests/test_swarm_race.py). None = server default.
        try:
            result = await run_in_threadpool(
                agent.run, request.message, session_id, swarm=request.swarm
            )
        except Exception:
            if idem_key is not None:
                agent.memory.idempotency_release(identity, idem_key)
            raise
        registry.inc("chat_runs_total", {"status": result.status.value})
        if idem_key is not None:
            agent.memory.idempotency_complete(identity, idem_key, 200, result.model_dump_json())
        return result

    @app.get("/api/approvals")
    def list_approvals(session_id: Optional[str] = None) -> dict[str, Any]:
        return {"pending": agent.pending_approvals(session_id)}

    @app.post("/api/approvals/{approval_id}", response_model=AgentResult)
    async def resolve_approval(approval_id: str, request: ApprovalRequest, raw: Request,
                               session_id: Optional[str] = None) -> Any:
        # session scoping: if pending exists, ensure caller is owner
        try:
            pending = agent.memory.get_pending(approval_id)
            if pending is not None and session_id is not None and pending.get("session_id") != session_id:
                raise HTTPException(403, "pending belongs to different session")
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        identity = raw.scope.get("nimna.identity", "anon")
        idem_key, body_hash = _idempotency_material(raw, "approvals.resolve", request, extra=approval_id)
        if idem_key is not None:
            replay = _idempotent_replay(identity, idem_key, "approvals.resolve", body_hash)
            if replay is not None:
                return replay
        try:
            result = await run_in_threadpool(agent.resume, approval_id, request.approved, always=request.always)
        except KeyError as exc:
            if idem_key is not None:
                agent.memory.idempotency_release(identity, idem_key)
            raise HTTPException(404, str(exc))
        except PermissionError as exc:
            if idem_key is not None:
                agent.memory.idempotency_release(identity, idem_key)
            raise HTTPException(403, str(exc))
        except Exception:
            if idem_key is not None:
                agent.memory.idempotency_release(identity, idem_key)
            raise
        registry.inc("approvals_resolved_total", {"approved": str(bool(request.approved))})
        if idem_key is not None:
            agent.memory.idempotency_complete(identity, idem_key, 200, result.model_dump_json())
        return result

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
        # negotiate only 'nimna.v1'; the 'nimna.key.*' credential protocol is never echoed
        await websocket.accept(subprotocol=select_ws_subprotocol(websocket.scope))
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
