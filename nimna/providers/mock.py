"""Offline provider used by the test-suite and for demoing the UI without a key.

* Scripted mode: pass a list of responses (``ModelResponse``/``str``/callable);
  they are consumed in order.
* Default mode (no script): returns a descriptive canned answer that reports
  which skills and tools were made available, so the routing layer can be
  exercised end-to-end without any network access.
"""
import re
from typing import Any, Callable, Optional, Union

from .base import Message, ModelProvider, ModelResponse, ToolSpec

Scripted = Union[ModelResponse, str, Callable[[list[Message], Optional[list[ToolSpec]]], Any]]


class MockProvider(ModelProvider):
    name = "mock"
    model = "mock"

    def __init__(self, responses: Optional[list[Scripted]] = None):
        self._responses: list[Scripted] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def queue(self, *responses: Scripted) -> None:
        self._responses.extend(responses)

    def generate(self, messages: list[Message], tools: Optional[list[ToolSpec]] = None, *,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> ModelResponse:
        self.calls.append({"messages": [m.model_copy() for m in messages], "tools": list(tools or [])})
        if self._responses:
            item = self._responses.pop(0)
            if callable(item) and not isinstance(item, ModelResponse):
                item = item(messages, tools)
            if isinstance(item, str):
                return ModelResponse(text=item)
            if isinstance(item, ModelResponse):
                return item
            raise TypeError(f"unsupported scripted response: {type(item)!r}")
        return self._default_answer(messages, tools)

    @staticmethod
    def _default_answer(messages: list[Message], tools: Optional[list[ToolSpec]]) -> ModelResponse:
        system = next((m.content for m in messages if m.role == "system"), "")
        skills = re.findall(r"^### Skill: (.+)$", system, flags=re.MULTILINE)
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        lines = [
            "(mock provider – no real model is configured)",
            "",
            f"Request: {last_user[:200]}",
            f"Skills loaded: {', '.join(skills) if skills else 'none'}",
            f"Tools available: {', '.join(t.name for t in (tools or [])) or 'none'}",
            "",
            "Set GEMINI_API_KEY (or OPENAI_API_KEY + OPENAI_BASE_URL) and MODEL_PROVIDER "
            "to get real answers.",
        ]
        return ModelResponse(text="\n".join(lines))
