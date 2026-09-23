"""Provider-agnostic message / tool-call types and the ModelProvider interface.

The agent core only ever talks to :class:`ModelProvider`; concrete providers
(Gemini, OpenAI-compatible such as NVIDIA NIM, mock) translate to and from the
vendor formats.  Swapping the model therefore never touches skills or tools.
"""
import copy
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant", "tool"]


def new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:10]


class ToolCall(BaseModel):
    id: str = Field(default_factory=new_call_id)
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # for role == "tool"
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    # provider specific payload for faithful replay (e.g. Gemini `Content`
    # including thought signatures). JSON serialisable.
    raw: Optional[dict[str, Any]] = None

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Optional[list[ToolCall]] = None,
                  raw: Optional[dict[str, Any]] = None) -> "Message":
        return cls(role="assistant", content=content, tool_calls=tool_calls or [], raw=raw)

    @classmethod
    def tool_result(cls, call: ToolCall, content: str) -> "Message":
        return cls(role="tool", content=content, tool_call_id=call.id, name=call.name)


class ToolSpec(BaseModel):
    """JSON-schema description of a tool, as exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: Optional[dict[str, Any]] = None
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: Optional[str] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> Message:
        return Message.assistant(self.text, self.tool_calls, self.raw)


class ProviderError(Exception):
    """Base error for provider failures."""

    def __init__(self, message: str, *, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class RateLimitError(ProviderError):
    def __init__(self, message: str = "rate limited", status: Optional[int] = 429):
        super().__init__(message, retryable=True, status=status)


T = TypeVar("T")


def with_retries(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 2.0,
                 max_delay: float = 30.0) -> T:
    """Call ``fn`` retrying on retryable :class:`ProviderError` (429/5xx).

    Free tiers rate-limit aggressively, so exponential back-off is essential.
    """
    delay = base_delay
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except ProviderError as exc:  # noqa: PERF203 - clarity over speed here
            last = exc
            if not exc.retryable or attempt == attempts:
                raise
            log.warning("provider error (%s), retry %d/%d in %.1fs", exc, attempt, attempts, delay)
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    assert last is not None
    raise last


class ModelProvider(ABC):
    """Interface every model backend implements."""

    name: str = "base"
    model: str = ""

    @abstractmethod
    def generate(self, messages: list[Message], tools: Optional[list[ToolSpec]] = None, *,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> ModelResponse:
        """Run one model turn. ``messages`` may start with a system message."""

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}


# ---------------------------------------------------------------------------
# JSON-schema helpers shared by providers
# ---------------------------------------------------------------------------

def _resolve_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            key = ref.split("/")[-1]
            target = copy.deepcopy(defs.get(key, {}))
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return _resolve_refs(merged, defs)
        return {k: _resolve_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, defs) for item in node]
    return node


def normalize_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs`` and drop noise pydantic adds (``title``/``default``).

    The result is still standard JSON schema (accepted by OpenAI-style APIs).
    """
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {}) or {}
    schema = _resolve_refs(schema, defs)

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in {"title", "default", "additionalProperties"}:
                    continue
                out[key] = clean(value)
            return out
        if isinstance(node, list):
            return [clean(item) for item in node]
        return node

    cleaned = clean(schema)
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


_GEMINI_ALLOWED_KEYS = {
    "type", "format", "description", "nullable", "enum", "items", "properties",
    "required", "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength",
}


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a normalised JSON schema to the OpenAPI subset Gemini accepts.

    * ``anyOf: [{type: X}, {type: null}]`` -> ``{type: X, nullable: true}``
    * unknown keywords are dropped
    * ``format`` is only kept for values Gemini understands
    """

    def convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        node = dict(node)
        variants = node.pop("anyOf", None) or node.pop("oneOf", None)
        if variants:
            non_null = [v for v in variants if v.get("type") != "null"]
            nullable = len(non_null) != len(variants)
            base = convert(non_null[0]) if non_null else {"type": "string"}
            if len(non_null) > 1:
                # Gemini has no union types; fall back to string with a hint
                base = {"type": "string", "description": node.get("description", "")}
            merged = {**base, **{k: v for k, v in node.items() if k != "description"}}
            if node.get("description"):
                merged["description"] = node["description"]
            if nullable:
                merged["nullable"] = True
            node = merged
        if isinstance(node.get("type"), list):
            types = [t for t in node["type"] if t != "null"]
            node["type"] = types[0] if types else "string"
            node["nullable"] = True
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key not in _GEMINI_ALLOWED_KEYS:
                continue
            if key == "properties":
                out[key] = {name: convert(prop) for name, prop in value.items()}
            elif key == "items":
                out[key] = convert(value)
            elif key == "format":
                if value in {"enum", "date-time", "int32", "int64", "float", "double"}:
                    out[key] = value
            else:
                out[key] = value
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        if out.get("type") == "array" and "items" not in out:
            out["items"] = {"type": "string"}
        return out

    return convert(schema)
