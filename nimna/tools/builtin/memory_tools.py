"""Long-term memory tools (SQLite backed)."""
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..base import ToolContext, ToolError, ToolRegistry


class MemorySaveParams(BaseModel):
    content: str = Field(..., min_length=2, max_length=2000, description="Fact, result or preference to remember.")
    kind: Literal["note", "preference", "result"] = Field("note", description="preference = user preference injected into future prompts.")
    tags: list[str] = Field(default_factory=list, description="Optional tags for retrieval.")


class MemorySearchParams(BaseModel):
    query: str = Field(..., min_length=1, description="Keywords to look for.")
    limit: int = Field(5, ge=1, le=20)
    kind: Optional[Literal["note", "preference", "result"]] = None


def register(registry: ToolRegistry) -> None:
    @registry.tool("memory_save", "Store a note, task result or user preference in long-term memory.",
                   MemorySaveParams, tags=["memory"])
    def memory_save(params: MemorySaveParams, ctx: ToolContext):
        if ctx.memory is None:
            raise ToolError("memory store is not available")
        memory_id = ctx.memory.save_memory(params.content, kind=params.kind, tags=params.tags,
                                           session_id=ctx.session_id)
        return {"saved": True, "id": memory_id, "kind": params.kind}

    @registry.tool("memory_search", "Search long-term memory (notes, previous results, preferences) by keywords.",
                   MemorySearchParams, tags=["memory"])
    def memory_search(params: MemorySearchParams, ctx: ToolContext):
        if ctx.memory is None:
            raise ToolError("memory store is not available")
        items = ctx.memory.search_memories(params.query, limit=params.limit)
        if params.kind:
            items = [item for item in items if item["kind"] == params.kind]
        return {
            "query": params.query,
            "results": [
                {"id": i["id"], "kind": i["kind"], "content": i["content"], "tags": i["tags"],
                 "created_at": i["created_at"]}
                for i in items
            ],
        }
