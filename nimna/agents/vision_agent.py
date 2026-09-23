"""VisionInspector — تحليل بصري + VisionCache."""
from __future__ import annotations

from .base import BaseSwarmAgent


class VisionAgent(BaseSwarmAgent):
    name = "vision"
    description = "Visual desktop inspection with VisionCache"
    allowed_tools = [
        "take_screenshot",
        "get_element_coordinates",
        "mouse_click",
        "type_text",
        "list_files",
        "read_file",
        "vector_memory_search",
        "vector_memory_save",
    ]
    system_prompt = """أنت VisionInspector — متخصص التحليل البصري للواجهات.

قواعد:
1. التقط الشاشة (take_screenshot) ثم حدد العنصر (get_element_coordinates) قبل أي نقر/كتابة.
2. استفد من VisionGateway: الشاشة تُحقن تلقائياً — حلّلها وخطط.
3. النقر/الكتابة يتطلبان purpose واضحاً — اذكر الغرض.
4. تحقق بعد كل إجراء بلقطة جديدة.
5. احفظ الأنماط البصرية المتكررة (hash) في الذاكرة إن كانت مفيدة.
"""

    # Vision advantage is already in parent Agent via VisionCache + hash dedup;
    # this agent is intentionally lean — its value is tool restriction + prompt.
