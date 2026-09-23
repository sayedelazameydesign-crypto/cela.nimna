"""Gemini provider built on the official ``google-genai`` SDK.

Works with the free tier of the Gemini API (https://aistudio.google.com).
The SDK is imported lazily so the rest of the package works without it.
"""
import json
import logging
from typing import Any, Optional

from .base import (
    Message,
    ModelProvider,
    ModelResponse,
    ProviderError,
    RateLimitError,
    ToolCall,
    ToolSpec,
    to_gemini_schema,
    with_retries,
)

log = logging.getLogger(__name__)


def _as_response_dict(content: str) -> dict[str, Any]:
    """Gemini requires function responses to be JSON objects."""
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return {"result": content}
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


class GeminiProvider(ModelProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", *,
                 temperature: float = 0.2, timeout: float = 120.0):
        if not api_key:
            raise ProviderError(
                "Gemini API key is not set – set GEMINI_API_KEY (preferred) or GOOGLE_API_KEY"
            )
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "google-genai is not installed. Run: pip install google-genai"
            ) from exc
        self._types = types
        http_options = types.HttpOptions(timeout=int(timeout * 1000))
        self.client = genai.Client(api_key=api_key, http_options=http_options)
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    # -- conversion ------------------------------------------------------
    def _to_contents(self, messages: list[Message]):
        t = self._types
        system_parts: list[str] = []
        contents: list[Any] = []
        pending_tool_parts: list[Any] = []

        def flush_tool_parts() -> None:
            if pending_tool_parts:
                contents.append(t.Content(role="tool", parts=list(pending_tool_parts)))
                pending_tool_parts.clear()

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
                continue
            if msg.role == "tool":
                pending_tool_parts.append(
                    t.Part.from_function_response(
                        name=msg.name or "tool", response=_as_response_dict(msg.content)
                    )
                )
                continue
            flush_tool_parts()
            if msg.role == "user":
                contents.append(
                    t.Content(role="user", parts=[t.Part.from_text(text=msg.content or " ")])
                )
            elif msg.role == "assistant":
                content = None
                if msg.raw:
                    try:
                        # replaying the exact model turn keeps thought signatures intact
                        content = t.Content.model_validate(msg.raw)
                    except Exception:  # pragma: no cover - defensive
                        content = None
                if content is None:
                    parts = []
                    if msg.content:
                        parts.append(t.Part.from_text(text=msg.content))
                    for call in msg.tool_calls:
                        parts.append(t.Part.from_function_call(name=call.name, args=call.arguments))
                    if not parts:
                        parts.append(t.Part.from_text(text=" "))
                    content = t.Content(role="model", parts=parts)
                contents.append(content)
        flush_tool_parts()
        system = "\n\n".join(system_parts) if system_parts else None
        return system, contents

    def _to_tools(self, tools: list[ToolSpec]):
        t = self._types
        declarations = [
            t.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters=to_gemini_schema(spec.parameters),
            )
            for spec in tools
        ]
        return [t.Tool(function_declarations=declarations)]

    # -- main call -------------------------------------------------------
    def generate(self, messages: list[Message], tools: Optional[list[ToolSpec]] = None, *,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> ModelResponse:
        t = self._types
        system, contents = self._to_contents(messages)
        config_kwargs: dict[str, Any] = {
            "temperature": self.temperature if temperature is None else temperature,
        }
        if max_tokens:
            config_kwargs["max_output_tokens"] = max_tokens
        if system:
            config_kwargs["system_instruction"] = system
        if tools:
            config_kwargs["tools"] = self._to_tools(tools)
            config_kwargs["automatic_function_calling"] = t.AutomaticFunctionCallingConfig(
                disable=True
            )
        config = t.GenerateContentConfig(**config_kwargs)

        def call():
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except Exception as exc:  # map SDK errors onto our hierarchy
                raise self._map_error(exc) from exc

        response = with_retries(call)
        return self._parse(response)

    @staticmethod
    def _map_error(exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        text = str(exc)
        lowered = text.lower()
        if code == 429 or "resource_exhausted" in lowered or "429" in text[:60] or "quota" in lowered:
            return RateLimitError(f"Gemini rate limit: {text[:300]}")
        # timeouts are retryable
        if "deadline" in lowered or "timeout" in lowered or "timed out" in lowered:
            return ProviderError(f"Gemini timeout: {text[:300]}", retryable=True, status=code if isinstance(code, int) else None)
        if isinstance(code, int) and code >= 500:
            return ProviderError(f"Gemini server error: {text[:300]}", retryable=True, status=code)
        # handle 400 due to invalid function call schema
        if isinstance(code, int) and code >= 400:
            return ProviderError(f"Gemini error ({code}): {text[:500]}", status=code)
        # SDK sometimes throws without code but with retryable hint
        if "500" in text or "503" in text or "unavailable" in lowered:
            return ProviderError(f"Gemini transient error: {text[:300]}", retryable=True)
        return ProviderError(f"Gemini error: {text[:500]}", status=code if isinstance(code, int) else None)

    def _parse(self, response: Any) -> ModelResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: Optional[dict[str, Any]] = None
        finish_reason: Optional[str] = None

        candidates = getattr(response, "candidates", None) or []
        candidate = candidates[0] if candidates else None
        if candidate is not None:
            finish_reason = str(getattr(candidate, "finish_reason", None) or "") or None
            content = getattr(candidate, "content", None)
            if content is not None:
                try:
                    raw = content.to_json_dict() if hasattr(content, "to_json_dict") else \
                        content.model_dump(mode="json", exclude_none=True)
                except Exception:  # pragma: no cover - defensive
                    raw = None
                for part in getattr(content, "parts", None) or []:
                    function_call = getattr(part, "function_call", None)
                    if function_call is not None:
                        tool_calls.append(
                            ToolCall(
                                id=getattr(function_call, "id", None) or ToolCall().id,
                                name=function_call.name,
                                arguments=dict(function_call.args or {}),
                            )
                        )
                    elif getattr(part, "text", None) and not getattr(part, "thought", False):
                        text_parts.append(part.text)

        usage: dict[str, int] = {}
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            for src, dst in (("prompt_token_count", "prompt_tokens"),
                             ("candidates_token_count", "completion_tokens"),
                             ("total_token_count", "total_tokens")):
                value = getattr(meta, src, None)
                if isinstance(value, int):
                    usage[dst] = value

        if not text_parts and not tool_calls:
            # blocked / empty candidates: surface the reason instead of silence
            feedback = getattr(response, "prompt_feedback", None)
            if feedback is not None and getattr(feedback, "block_reason", None):
                text_parts.append(f"[blocked by safety filters: {feedback.block_reason}]")
            # handle invalid model JSON case gracefully – return whatever text exists
            if finish_reason in {"MALFORMED_FUNCTION_CALL", "ERROR"}:
                text_parts.append(f"[model returned invalid tool call: {finish_reason}]")

        return ModelResponse(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            raw=raw,
            usage=usage,
            finish_reason=finish_reason,
        )
