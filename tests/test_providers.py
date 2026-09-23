import json

import httpx
import pytest

from nimna.providers.base import Message, ProviderError, RateLimitError, ToolCall, ToolSpec, with_retries
from nimna.providers.openai_compat import OpenAICompatibleProvider

SPEC = ToolSpec(name="read_csv", description="d",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})


def make_provider(handler):
    provider = OpenAICompatibleProvider("KEY", "https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct")
    provider.client = httpx.Client(base_url=provider.base_url, headers=provider.client.headers,
                                   transport=httpx.MockTransport(handler))
    return provider


def test_openai_compatible_wire_format_and_parsing():
    captured = {}

    def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_csv", "arguments": "{\"path\": \"sales.csv\"}"}}]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    provider = make_provider(handler)
    response = provider.generate([
        Message.system("s"), Message.user("u"),
        Message.assistant("", [ToolCall(id="c0", name="list_files", arguments={})]),
        Message.tool_result(ToolCall(id="c0", name="list_files"), '{"count": 1}'),
    ], tools=[SPEC])

    assert captured["auth"] == "Bearer KEY"
    body = captured["body"]
    assert body["model"] == "meta/llama-3.3-70b-instruct"
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "list_files", "arguments": "{}"}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": '{"count": 1}'}
    assert body["tools"][0]["function"]["name"] == "read_csv" and body["tool_choice"] == "auto"
    assert response.tool_calls[0].arguments == {"path": "sales.csv"}
    assert response.usage["total_tokens"] == 15 and response.finish_reason == "tool_calls"


def test_openai_compatible_errors():
    provider = make_provider(lambda request: httpx.Response(401, text="bad key"))
    with pytest.raises(ProviderError) as exc:
        provider.generate([Message.user("hi")])
    assert exc.value.status == 401 and not exc.value.retryable

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    provider = make_provider(flaky)
    import nimna.providers.base as base
    original = base.time.sleep
    base.time.sleep = lambda s: None  # don't actually wait in tests
    try:
        assert provider.generate([Message.user("hi")]).text == "ok"
    finally:
        base.time.sleep = original
    assert calls["n"] == 3


def test_with_retries_gives_up_on_non_retryable():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise ProviderError("nope")

    with pytest.raises(ProviderError):
        with_retries(fn, attempts=3, base_delay=0)
    assert attempts["n"] == 1

    def fn2():
        attempts["n"] += 1
        raise RateLimitError()

    with pytest.raises(RateLimitError):
        with_retries(fn2, attempts=2, base_delay=0)
    assert attempts["n"] == 3
