"""CodeAgent — كتابة/تنفيذ/تصحيح كود + Self-Healing عبر execution_history."""
from __future__ import annotations

import logging

from ..providers.base import Message
from .base import BaseSwarmAgent

log = logging.getLogger(__name__)


class CodeAgent(BaseSwarmAgent):
    name = "code"
    description = "Code writing, execution and self-healing"
    allowed_tools = [
        "shell_execute",
        "run_python",
        "write_file",
        "read_file",
        "list_files",
        "file_info",
        "vector_memory_search",
        "vector_memory_save",
        "memory_search",
    ]
    system_prompt = """أنت CodeAgent — مهندس كود وتنفيذ معزول مع تعافٍ ذاتي.

قواعد:
1. اكتب الكود ثم نفّذه بـ run_python أو shell_execute داخل workspace فقط.
2. لا تستخدم الشبكة ولا تحاول الهروب من workspace. لا تحذف ملفات لم يُطلب حذفها.
3. عند فشل التنفيذ (stderr/exception): اقرأ الخطأ، استعلم في الذاكرة المتجهية (vector_memory_search في execution_history) عن أخطاء مشابهة وحلولها، ثم عدّل الكود وحاول مجدداً — حتى 3 محاولات.
4. عند نجاح حل صعب، احفظ النمط (الخطأ→الحل) في execution_history عبر vector_memory_save ليتعلمه النظام.
5. اكتب المخرجات في ملفات جديدة ولا تكتب فوق الأصلي إلا عند الطلب الصريح.
"""

    def _on_tool_error(self, task: str, call, result: str, messages: list[Message], context) -> bool:
        """Self-Healing: on stderr/exception, query vector memory for similar fixes."""
        # only heal for execution tools
        if call.name not in ("shell_execute", "run_python"):
            return False
        # avoid infinite loop: count retries via messages
        retry_count = sum(1 for m in messages if m.role == "user" and "[Self-Healing" in (m.content or ""))
        if retry_count >= 3:
            return False
        # extract error snippet
        snippet = result[:800] if isinstance(result, str) else str(call.arguments)[:800]
        # heuristic: look for common patterns
        if "ModuleNotFoundError" in snippet or "Traceback" in snippet or "error" in snippet.lower() or "denied" in snippet.lower():
            try:
                from nimna.memory.qdrant import get_vector_memory

                vm = get_vector_memory()
                # embed dims fallback handles offline
                hits = vm.search(snippet[:500], collections=["execution_history", "code_knowledge"], limit=3)
                if hits:
                    fixes = "\n".join(f"- ({h['collection']} {h['score']:.2f}) {h['text'][:250]}" for h in hits)
                    hint = (
                        f"[Self-Healing {retry_count+1}/3] فشل التنفيذ: {snippet[:400]}\n"
                        f"اقتراحات من الذاكرة المتجهية (execution_history):\n{fixes}\n"
                        f"حاول إصلاح الكود بناءً على الاقتراحات ثم أعد التنفيذ. لا تطلب موافقة إضافية."
                    )
                    messages.append(Message.user(hint))
                    # also audit
                    try:
                        # we don't have direct session/run here; log via parent if possible
                        pass
                    except Exception:
                        pass
                    return True
            except Exception as exc:
                log.debug("self-healing vector search failed: %s", exc)
            # even without hits, give generic retry hint
            messages.append(
                Message.user(
                    f"[Self-Healing {retry_count+1}/3] فشل: {snippet[:400]} — حاول إصلاح الأخطاء (تثبيت حزمة، تصحيح مسار، تعديل syntax) ثم أعد التنفيذ."
                )
            )
            return True
        return False

    def run(self, task: str, session_id=None, context=None):
        # wrap base run and on success store execution pattern if valuable
        result = super().run(task, session_id=session_id, context=context)
        if result.ok and result.tool_calls:
            # if we executed code successfully after retries, store pattern
            try:
                has_code = any(c.get("name") in ("run_python", "shell_execute") for c in result.tool_calls)
                has_retry = any("Self-Healing" in (str(c)) for c in result.tool_calls)
                if has_code and len(result.tool_calls) > 1:
                    from nimna.memory.qdrant import get_vector_memory

                    vm = get_vector_memory()
                    # store anonymized pattern (task + output snippet)
                    pattern = f"Task: {task[:300]}\nResult: {result.output[:400]}"
                    vm.upsert("execution_history", pattern, tags=["self-healing", "success"])
            except Exception:
                pass
        return result
