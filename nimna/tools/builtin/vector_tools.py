"""Vector Memory tools — Qdrant + fallback (Sprint 2).

These tools give the LLM semantic recall beyond SQLite LIKE search:
  - vector_memory_save  -> user_context / execution_history / code_knowledge
  - vector_memory_search -> semantic cosine search

Contract: collection ∈ {user_context, execution_history, code_knowledge}
- user_context: تفضيلات/أنماط/قرارات
- execution_history: سجلات Shell للـ Self-Healing (الأخطاء + الحلول)
- code_knowledge: snippets والحلول الشائعة
"""
from typing import Optional

from pydantic import BaseModel, Field

from ..base import ToolContext, ToolError, ToolRegistry

# collections are defined in nimna.memory.qdrant.COLLECTIONS


class VectorSaveParams(BaseModel):
    collection: str = Field(..., description="user_context | execution_history | code_knowledge")
    text: str = Field(..., min_length=3, max_length=8000, description="النص المراد حفظه دلالياً")
    tags: list[str] = Field(default_factory=list, description="وسوم للتصفية لاحقاً")
    metadata: Optional[dict] = Field(None, description="بيانات إضافية (اختياري)")


class VectorSearchParams(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="استعلام دلالي")
    collections: Optional[list[str]] = Field(None, description="قائمة collections للبحث (افتراضياً الكل)")
    limit: int = Field(5, ge=1, le=20)


def register(registry: ToolRegistry) -> None:
    @registry.tool(
        "vector_memory_save",
        "حفظ معلومة دلالية في الذاكرة المتجهية (semantic) — تُستخدم للاسترجاع طويل الأمد عبر التشابه المعنوي. collection: user_context | execution_history | code_knowledge",
        VectorSaveParams,
        tags=["memory", "vector"],
    )
    def vector_memory_save(params: VectorSaveParams, ctx: ToolContext):
        try:
            from nimna.memory.qdrant import COLLECTIONS, get_vector_memory
        except Exception as exc:
            raise ToolError(f"vector memory unavailable: {exc}")
        if params.collection not in COLLECTIONS:
            raise ToolError(f"unknown collection '{params.collection}'; valid: {list(COLLECTIONS)}")
        vm = get_vector_memory()
        rid = vm.upsert(params.collection, params.text, metadata=params.metadata, tags=params.tags)
        return {"saved": True, "id": rid, "collection": params.collection}

    @registry.tool(
        "vector_memory_search",
        "بحث دلالي (semantic) في الذاكرة المتجهية — يسترجع تفضيلات/حلول/أخطاء سابقة بالمعنى وليس الكلمات المفتاحية فقط. أقوى من memory_search للسياق طويل الأمد.",
        VectorSearchParams,
        tags=["memory", "vector"],
    )
    def vector_memory_search(params: VectorSearchParams, ctx: ToolContext):
        try:
            from nimna.memory.qdrant import get_vector_memory
        except Exception as exc:
            raise ToolError(f"vector memory unavailable: {exc}")
        vm = get_vector_memory()
        results = vm.search(params.query, collections=params.collections, limit=params.limit)
        return {
            "query": params.query,
            "results": [
                {
                    "collection": r["collection"],
                    "id": r["id"],
                    "text": r["text"],
                    "score": r["score"],
                    "payload": r["payload"],
                }
                for r in results
            ],
        }

    class VectorHealthParams(BaseModel):
        pass

    @registry.tool(
        "vector_memory_health",
        "فحص حالة الذاكرة المتجهية (provider, counts, embedding model)",
        VectorHealthParams,
        tags=["memory", "vector"],
    )
    def vector_memory_health(params: VectorHealthParams, ctx: ToolContext):  # type: ignore
        try:
            from nimna.memory.qdrant import get_vector_memory
            return get_vector_memory().health()
        except Exception as exc:
            raise ToolError(str(exc))
