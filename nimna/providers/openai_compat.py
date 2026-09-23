"""Provider for any OpenAI-compatible ``/chat/completions`` endpoint.

Covers NVIDIA NIM (``https://integrate.api.nvidia.com/v1``), OpenAI, Groq,
OpenRouter, Together, vLLM, Ollama, LM Studio ... only the base URL, key and
model name change.  Implemented with ``httpx`` to avoid a heavy SDK.
"""
import json
import logging
from typing import Any, Optional

import httpx

from .base import (
    Message,
    ModelProvider,
    ModelResponse,
    ProviderError,
    RateLimitError,
    ToolCall,
    ToolSpec,
    with_retries,
)

log = logging.getLogger(__name__)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        # model returned non-JSON arguments – surface as _raw so agent can handle
        return {"_raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


class OpenAICompatibleProvider(ModelProvider):
    name = "openai"

    def __init__(self, api_key: str, base_url: str, model: str, *, temperature: float = 0.2,
                 timeout: float = 120.0, extra_headers: Optional[dict[str, str]] = None,
                 extra_body: Optional[dict[str, Any]] = None):
        if not base_url:
            raise ProviderError("OPENAI_BASE_URL is not set")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)
        self.model = model
        self.temperature = temperature
        self.extra_body = extra_body or {}

    # -- conversion ------------------------------------------------------
    @staticmethod
    def _to_wire(msg: Message) -> dict[str, Any]:
        if msg.role == "tool":
            return {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
        if msg.role == "assistant":
            wire: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                wire["content"] = msg.content or None
                wire["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in msg.tool_calls
                ]
            return wire
        return {"role": msg.role, "content": msg.content}

    # -- main call -------------------------------------------------------
    def generate(self, messages: list[Message], tools: Optional[list[ToolSpec]] = None, *,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._to_wire(m) for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
                for spec in tools
            ]
            payload["tool_choice"] = "auto"
        payload.update(self.extra_body)

        def call() -> dict[str, Any]:
            try:
                response = self.client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderError(f"timeout talking to {self.base_url}: {exc}", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"HTTP error talking to {self.base_url}: {exc}", retryable=True) from exc
            if response.status_code == 429:
                raise RateLimitError(f"rate limited by {self.base_url}: {response.text[:300]}")
            if response.status_code >= 500:
                raise ProviderError(
                    f"server error {response.status_code}: {response.text[:300]}",
                    retryable=True, status=response.status_code,
                )
            if response.status_code >= 400:
                body = response.text[:600]
                # some models return 400 when tools are unsupported – surface clearly
                if "tool" in body.lower() or "function" in body.lower():
                    log.warning("provider rejected tools payload: %s", body[:300])
                raise ProviderError(
                    f"request rejected ({response.status_code}): {body}",
                    status=response.status_code,
                )
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderError(f"invalid JSON from provider: {response.text[:300]}") from exc

        data = with_retries(call)
        return self._parse(data)

    @staticmethod
    def _parse(data: dict[str, Any]) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            # some providers return error as choices=[] but with error field – surface it
            err = data.get("error") or data
            raise ProviderError(f"provider returned no choices: {json.dumps(err)[:400]}")
        choice = choices[0]
        message = choice.get("message") or {}
        # handle case where model does not support tool_calls: it may return content only
        raw_tool_calls = message.get("tool_calls")
        tool_calls: list[ToolCall] = []
        if raw_tool_calls:
            for call in raw_tool_calls:
                fn = (call.get("function") or {})
                name = fn.get("name")
                if not name:
                    continue
                args = _parse_arguments(fn.get("arguments"))
                # if model emitted invalid JSON for arguments, keep but warn
                if "_raw" in args:
                    log.warning("model emitted non-JSON tool arguments for %s: %r", name, fn.get("arguments"))
                tool_calls.append(ToolCall(id=call.get("id") or ToolCall().id, name=name, arguments=args))
        content = message.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        elif content is None:
            content = ""
        usage = {k: v for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
        return ModelResponse(
            text=str(content).strip(),
            tool_calls=tool_calls,
            raw=None,
            usage=usage,
            finish_reason=choice.get("finish_reason"),
        )
