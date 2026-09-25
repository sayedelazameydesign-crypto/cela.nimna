"""SearchAgent — يجمع البيانات الخارجية + يسترجع سياقًا سابقًا دلالياً."""
from __future__ import annotations

from .base import BaseSwarmAgent


class SearchAgent(BaseSwarmAgent):
    name = "search"
    description = "Research & web retrieval with vector context"
    allowed_tools = [
        "web_search",
        "fetch_url",
        "vector_memory_search",
        "vector_memory_save",
        "memory_search",
        "memory_save",
        "list_files",
        "read_file",
    ]
    system_prompt = """أنت SearchAgent — خبير بحث وتجميع المعلومات.

قواعد:
1. ابحث أولاً في الذاكرة المتجهية (vector_memory_search) عن سياق سابق ذي صلة، ثم في الويب (web_search/fetch_url) إذا لزم.
2. استشهد بمصادرك (عناوين URL) ولا تخترع حقائق.
3. إذا وجدت معلومة قيمة ومستقرة (تفضيل، حل، قرار)، احفظها في الذاكرة المتجهية (vector_memory_save) مع tags مناسبة — collection المناسب: user_context للسياق، code_knowledge للحلول.
4. كن موجزًا لكن دقيقًا؛ أعطِ جدولًا إن كانت بيانات.
"""
