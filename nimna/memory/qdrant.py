"""Vector Memory — Qdrant + in-memory fallback (Sprint 2).

Collections (Contract for Multi-Agent Swarm):
  - user_context        : تفضيلات البرمجة والأنماط المعمارية وقرارات المشروع
  - execution_history   : الأوامر وسجلات Shell (نجاح/فشل) → يغذي Self-Healing
  - code_knowledge      : المقتطعات البرمجية والحلول الشائعة

Design:
  - Provider = auto: try Gemini embeddings if GEMINI_API_KEY + embedding_provider != hash,
    else deterministic hash embeddings (768-dim, L2 normalized, no external deps).
  - Qdrant client is optional: if QDRANT_URL set and qdrant-client installed and reachable,
    use Qdrant; otherwise fallback to In-Memory Cosine Store (fully functional offline).
  - All writes go to both? For MVP fallback-only writes to in-memory if Qdrant unavailable;
    when Qdrant becomes available later, caller can .migrate() if needed.

Usage:
    from nimna.memory.qdrant import get_vector_memory
    vm = get_vector_memory()
    vm.upsert("user_context", "أفضل استخدام pydantic للتحقق", tags=["python"])
    results = vm.search("كيف أتحقق من النماذج؟", collections=["user_context","code_knowledge"])
    vm.health()  -> {"enabled": True, "provider": "fallback", "dim": 768, ...}
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

COLLECTIONS = {
    "user_context": {
        "description": "تفضيلات البرمجة والأنماط المعمارية وقرارات المشروع",
        "vector_size": 768,
        "distance": "Cosine",
    },
    "execution_history": {
        "description": "الأوامر وسجلات Shell الناجحة والفاشلة — تغذية Self-Healing",
        "vector_size": 768,
        "distance": "Cosine",
    },
    "code_knowledge": {
        "description": "المقتطفات البرمجية والحلول الشائعة",
        "vector_size": 768,
        "distance": "Cosine",
    },
}

# -- embeddings --------------------------------------------------------------

def _hash_embedding(text: str, dim: int = 768) -> list[float]:
    """Deterministic, normalized hash embedding — no deps, stable across runs."""
    text = text[:8000]  # truncate
    vector: list[float] = []
    counter = 0
    while len(vector) < dim:
        chunk = hashlib.sha256(f"{text}:{counter}".encode()).digest()
        for i in range(0, len(chunk), 4):
            if len(vector) >= dim:
                break
            val = struct.unpack(">I", chunk[i:i+4])[0] / 0xFFFFFFFF  # 0..1
            vector.append((val * 2) - 1)  # -1..1
        counter += 1
        if counter > 1000:  # safety
            break
    # L2 normalize
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]

def _gemini_embedding(text: str, model: str = "text-embedding-004", dim: int = 768) -> list[float] | None:
    """Try Gemini embeddings; return None on failure (fallback to hash)."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        # gemini embedding: models/text-embedding-004
        m = model if "/" in model else f"models/{model}"
        result = client.models.embed_content(model=m, contents=[text])
        # result.embeddings[0].values
        emb = None
        if hasattr(result, "embeddings") and result.embeddings:
            emb = result.embeddings[0].values if hasattr(result.embeddings[0], "values") else result.embeddings[0]
            if hasattr(emb, "__iter__"):
                vec = list(emb)  # type: ignore
            else:
                vec = None
        elif hasattr(result, "embedding") and result.embedding:
            vec = list(result.embedding.values) if hasattr(result.embedding, "values") else list(result.embedding)  # type: ignore
        else:
            vec = None
        if vec is None:
            return None
        # normalize & pad/truncate to dim
        if len(vec) != dim:
            if len(vec) > dim:
                vec = vec[:dim]
            else:
                vec = vec + [0.0] * (dim - len(vec))
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]
    except Exception:
        return None

def _embed(text: str, model: str = "text-embedding-004", dim: int = 768, provider: str = "auto") -> list[float]:
    if provider in ("gemini", "auto"):
        if provider == "gemini" or (provider == "auto" and (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))):
            vec = _gemini_embedding(text, model=model, dim=dim)
            if vec is not None:
                return vec
        if provider == "gemini":
            # forced gemini but failed -> fallback anyway
            pass
    return _hash_embedding(text, dim=dim)

# -- fallback in-memory store ------------------------------------------------

@dataclass
class _Record:
    id: str
    text: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

def _cosine(a: list[float], b: list[float]) -> float:
    # both are L2 normalized → dot = cosine
    return sum(x * y for x, y in zip(a, b))

class _InMemoryStore:
    def __init__(self):
        self._data: dict[str, list[_Record]] = {name: [] for name in COLLECTIONS}
        self._lock = threading.RLock()

    def upsert(self, collection: str, text: str, vector: list[float], payload: dict[str, Any], id: str | None = None) -> str:
        with self._lock:
            if collection not in self._data:
                self._data[collection] = []
            rid = id or uuid.uuid4().hex[:12]
            # replace if exists
            self._data[collection] = [r for r in self._data[collection] if r.id != rid]
            self._data[collection].append(_Record(id=rid, text=text, vector=vector, payload=payload))
            return rid

    def search(self, query_vec: list[float], collections: list[str], limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            candidates: list[tuple[float, _Record, str]] = []
            for col in collections:
                for rec in self._data.get(col, []):
                    score = _cosine(query_vec, rec.vector)
                    candidates.append((score, rec, col))
            candidates.sort(key=lambda x: x[0], reverse=True)
            out = []
            for score, rec, col in candidates[:limit]:
                out.append({
                    "collection": col,
                    "id": rec.id,
                    "text": rec.text,
                    "score": round(float(score), 4),
                    "payload": rec.payload,
                    "created_at": rec.created_at,
                })
            return out

    def count(self, collection: str) -> int:
        with self._lock:
            return len(self._data.get(collection, []))

    def clear(self, collection: str | None = None) -> None:
        with self._lock:
            if collection:
                self._data[collection] = []
            else:
                for k in self._data:
                    self._data[k] = []

    def collections(self) -> dict[str, int]:
        with self._lock:
            return {k: len(v) for k, v in self._data.items()}

# -- main provider -----------------------------------------------------------

class VectorMemory:
    """Unified Qdrant + fallback. All methods work offline."""

    def __init__(
        self,
        qdrant_url: str | None = None,
        qdrant_api_key: str | None = None,
        embedding_model: str = "text-embedding-004",
        embedding_dim: int = 768,
        embedding_provider: str = "auto",
    ):
        self.qdrant_url = qdrant_url or os.getenv("QDRANT_URL", "") or None
        self.qdrant_api_key = qdrant_api_key or os.getenv("QDRANT_API_KEY", "") or None
        self.embedding_model = embedding_model or os.getenv("EMBEDDING_MODEL", "text-embedding-004")
        self.embedding_dim = int(embedding_dim or os.getenv("EMBEDDING_DIM", "768") or 768)
        self.embedding_provider = (embedding_provider or os.getenv("EMBEDDING_PROVIDER", "auto") or "auto").lower()
        self._fallback = _InMemoryStore()
        self._qdrant = None
        self._qdrant_error: str | None = None
        self._init_qdrant()

    def _init_qdrant(self) -> None:
        if not self.qdrant_url:
            return
        try:
            from qdrant_client import QdrantClient  # type: ignore
            from qdrant_client.http.models import Distance, VectorParams  # type: ignore
            self._qdrant = QdrantClient(url=self.qdrant_url, api_key=self.qdrant_api_key, timeout=5)
            # ensure collections
            for name, meta in COLLECTIONS.items():
                try:
                    self._qdrant.get_collection(name)
                except Exception:
                    try:
                        self._qdrant.create_collection(
                            collection_name=name,
                            vectors_config=VectorParams(size=self.embedding_dim, distance=Distance.COSINE),
                        )
                    except Exception as e:
                        self._qdrant_error = str(e)[:200]
                        self._qdrant = None
                        break
        except Exception as e:
            self._qdrant_error = str(e)[:200]
            self._qdrant = None

    @property
    def provider(self) -> str:
        if self._qdrant is not None:
            return "qdrant"
        if self.qdrant_url:
            return "fallback"  # url set but unreachable
        return "fallback"

    @property
    def enabled(self) -> bool:
        return True  # always enabled (fallback guarantees)

    def _vector(self, text: str) -> list[float]:
        return _embed(text, model=self.embedding_model, dim=self.embedding_dim, provider=self.embedding_provider)

    def upsert(
        self,
        collection: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        id: str | None = None,
    ) -> str:
        if collection not in COLLECTIONS:
            raise ValueError(f"unknown collection '{collection}'; valid: {list(COLLECTIONS)}")
        if not text or not text.strip():
            raise ValueError("text must be non-empty")
        text = text[:8000]
        payload: dict[str, Any] = {
            "text": text,
            "tags": tags or [],
            "meta": metadata or {},
            "created_at": time.time(),
        }
        vector = self._vector(text)
        # try Qdrant first
        if self._qdrant is not None:
            try:
                from qdrant_client.http.models import PointStruct  # type: ignore
                rid = id or uuid.uuid4().hex[:12]
                # Qdrant requires int or UUID; we use uuid hex -> convert to proper uuid
                # use hex as string id via uuid string
                import uuid as _uuid
                qid = str(_uuid.UUID(int=int(rid[:32].ljust(32, "0"), 16))) if len(rid) >= 8 else str(_uuid.uuid4())
                # store original rid in payload
                payload["rid"] = rid
                self._qdrant.upsert(
                    collection_name=collection,
                    points=[PointStruct(id=qid, vector=vector, payload=payload)],
                )
                # also mirror to fallback for fast local search in tests
                self._fallback.upsert(collection, text, vector, payload, id=rid)
                return rid
            except Exception as e:
                self._qdrant_error = str(e)[:300]
                # fall through to fallback
        # fallback
        return self._fallback.upsert(collection, text, vector, payload, id=id)

    def search(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 5,
        min_score: float = 0.0,
        filter_tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        cols = collections or list(COLLECTIONS.keys())
        cols = [c for c in cols if c in COLLECTIONS]
        if not cols:
            return []
        qvec = self._vector(query)
        # try Qdrant
        if self._qdrant is not None:
            try:
                # query each collection and merge
                merged: list[dict[str, Any]] = []
                for col in cols:
                    res = self._qdrant.search(
                        collection_name=col,
                        query_vector=qvec,
                        limit=limit,
                        with_payload=True,
                    )
                    for pt in res:
                        payload = pt.payload or {}
                        score = float(pt.score) if hasattr(pt, "score") else 0.0
                        if filter_tags and not any(t in (payload.get("tags") or []) for t in filter_tags):
                            continue
                        if score < min_score:
                            continue
                        merged.append({
                            "collection": col,
                            "id": payload.get("rid") or str(pt.id),
                            "text": payload.get("text") or "",
                            "score": round(score, 4),
                            "payload": payload,
                            "created_at": payload.get("created_at"),
                        })
                merged.sort(key=lambda x: x["score"], reverse=True)
                if merged:
                    return merged[:limit]
            except Exception as e:
                self._qdrant_error = str(e)[:300]
                # fallback
        # fallback search
        results = self._fallback.search(qvec, cols, limit=limit * 2)
        if filter_tags:
            results = [r for r in results if any(t in (r["payload"].get("tags") or []) for t in filter_tags)]
        if min_score > 0:
            results = [r for r in results if r["score"] >= min_score]
        return results[:limit]

    def count(self, collection: str | None = None) -> dict[str, int] | int:
        if collection:
            if self._qdrant is not None:
                try:
                    info = self._qdrant.get_collection(collection)
                    return int(info.points_count or 0)  # type: ignore
                except Exception:
                    pass
            return self._fallback.count(collection)
        # all
        if self._qdrant is not None:
            out = {}
            for name in COLLECTIONS:
                try:
                    info = self._qdrant.get_collection(name)
                    out[name] = int(info.points_count or 0)  # type: ignore
                except Exception:
                    out[name] = self._fallback.count(name)
            return out
        return self._fallback.collections()

    def clear(self, collection: str | None = None) -> None:
        # clear fallback always
        self._fallback.clear(collection)
        if self._qdrant is not None:
            try:
                cols = [collection] if collection else list(COLLECTIONS.keys())
                for col in cols:
                    try:
                        self._qdrant.delete_collection(col)
                    except Exception:
                        pass
                    # recreate
                    from qdrant_client.http.models import Distance, VectorParams  # type: ignore
                    self._qdrant.create_collection(
                        collection_name=col,
                        vectors_config=VectorParams(size=self.embedding_dim, distance=Distance.COSINE),
                    )
            except Exception as e:
                self._qdrant_error = str(e)[:300]

    def health(self) -> dict[str, Any]:
        counts = self.count() if isinstance(self.count(), dict) else {}
        if isinstance(counts, int):
            counts = {}
        return {
            "enabled": True,
            "provider": self.provider,
            "qdrant_url": bool(self.qdrant_url),
            "qdrant_reachable": self._qdrant is not None,
            "qdrant_error": self._qdrant_error,
            "embedding_model": self.embedding_model,
            "embedding_provider": self.embedding_provider,
            "vector_dim": self.embedding_dim,
            "collections": COLLECTIONS,
            "counts": counts,
            "fallback_counts": self._fallback.collections(),
        }

    def list_collections(self) -> list[str]:
        return list(COLLECTIONS.keys())

# singleton
_singleton: VectorMemory | None = None
_lock = threading.Lock()

def get_vector_memory(
    qdrant_url: str | None = None,
    qdrant_api_key: str | None = None,
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
    embedding_provider: str | None = None,
) -> VectorMemory:
    global _singleton
    with _lock:
        if _singleton is None:
            from nimna.config import Settings
            try:
                s = Settings.from_env()
                _singleton = VectorMemory(
                    qdrant_url=qdrant_url if qdrant_url is not None else s.qdrant_url,
                    qdrant_api_key=qdrant_api_key if qdrant_api_key is not None else s.qdrant_api_key,
                    embedding_model=embedding_model or s.embedding_model,
                    embedding_dim=embedding_dim or s.embedding_dim,
                    embedding_provider=embedding_provider or s.embedding_provider,
                )
            except Exception:
                _singleton = VectorMemory(
                    qdrant_url=qdrant_url,
                    qdrant_api_key=qdrant_api_key,
                    embedding_model=embedding_model or "text-embedding-004",
                    embedding_dim=embedding_dim or 768,
                    embedding_provider=embedding_provider or "auto",
                )
        return _singleton

def reset_vector_memory() -> None:
    global _singleton
    with _lock:
        _singleton = None

def hash_embedding(text: str, dim: int = 768) -> list[float]:
    return _hash_embedding(text, dim=dim)
