"""Tool registry: typed, validated, permission-aware tools.

Each tool declares its parameters as a pydantic model.  Arguments coming from
the model are validated before execution; validation errors are returned to
the model so it can self-correct.  Every tool has a risk level:

* ``safe``    – read-only / idempotent, executed immediately
* ``confirm`` – needs explicit user approval (delete, overwrite, send, run ...)

A tool may compute its risk dynamically (e.g. writing a *new* file is safe,
overwriting an existing one requires confirmation).
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from ..providers.base import ToolSpec, normalize_json_schema

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..memory.store import MemoryStore
    from ..skills.manager import SkillManager

log = logging.getLogger(__name__)

Risk = Literal["safe", "confirm"]

# substrings that look like secrets – redacted before logging / showing to the model
_SECRET_RE = re.compile(r"(api[_-]?key|secret|password|token|bearer)", re.I)


def redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if _SECRET_RE.search(str(key)):
                out[key] = "***REDACTED***"
            elif isinstance(value, (dict, list)):
                out[key] = redact_payload(value)
            elif isinstance(value, str) and len(value) > 20 and _SECRET_RE.search(value):
                out[key] = "***REDACTED***"
            else:
                out[key] = redact_payload(value) if isinstance(value, (dict, list)) else value
        return out
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    return payload


class ToolError(Exception):
    """Raised by tools for expected failures; the message is shown to the model."""


class ToolValidationError(ToolError):
    pass


@dataclass
class ToolContext:
    """Everything a tool handler may need. Built per run by the agent."""

    settings: "Settings"
    workspace: Path
    session_id: str
    memory: Optional["MemoryStore"] = None
    skills: Optional["SkillManager"] = None
    run_id: str = ""
    # callback used by `load_skill` so the agent can widen the allowed tools
    on_skill_loaded: Optional[Callable[[str], None]] = None
    extras: dict[str, Any] = field(default_factory=dict)

    # -- filesystem jail -------------------------------------------------
    def resolve_path(self, relative: str, *, must_exist: bool = False) -> Path:
        """Resolve ``relative`` inside the workspace, refusing escapes.

        Rejects absolute paths, ``..`` traversals, and symlink escapes – the
        resolved target must stay inside the resolved workspace root.
        """
        if not relative or not str(relative).strip():
            raise ToolError("path is required")
        candidate = Path(str(relative).strip())
        # Block absolute paths – callers must use workspace-relative paths.
        if candidate.is_absolute():
            raise ToolError(f"absolute paths not allowed: '{relative}' – use a workspace-relative path")
        # Block null bytes and suspicious patterns early
        if "\x00" in str(relative):
            raise ToolError("path contains null bytes")
        root = self.workspace.resolve()
        # Resolve the candidate against the workspace; resolve() follows symlinks.
        # Use strict=False so non-existent paths still get normalised.
        try:
            target = (root / candidate).resolve()
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"invalid path '{relative}': {exc}") from exc
        # Jail check: target must be root or inside root
        if target != root and root not in target.parents:
            raise ToolError(
                f"path '{relative}' escapes the workspace ({root}); use a relative path inside the workspace"
            )
        if must_exist and not target.exists():
            raise ToolError(f"path '{relative}' does not exist in the workspace")
        return target

    def display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace.resolve())) or "."
        except ValueError:
            return str(path)


Handler = Callable[[BaseModel, ToolContext], Any]
RiskFn = Callable[[BaseModel, ToolContext], Risk]


@dataclass
class Tool:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: Handler
    risk: Risk = "safe"
    risk_fn: Optional[RiskFn] = None
    tags: list[str] = field(default_factory=list)

    def spec(self) -> ToolSpec:
        schema = normalize_json_schema(self.params_model.model_json_schema())
        return ToolSpec(name=self.name, description=self.description, parameters=schema)

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        try:
            return self.params_model.model_validate(arguments or {})
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'args'}: {err['msg']}" for err in exc.errors()
            )
            raise ToolValidationError(f"invalid arguments for {self.name}: {details}") from exc

    def effective_risk(self, params: BaseModel, ctx: ToolContext) -> Risk:
        if self.risk_fn is not None:
            try:
                return self.risk_fn(params, ctx)
            except Exception:  # pragma: no cover - be conservative
                return "confirm"
        return self.risk

    def run(self, params: BaseModel, ctx: ToolContext) -> Any:
        return self.handler(params, ctx)


def serialize_result(result: Any, max_chars: int = 12000) -> str:
    """Turn a tool result into the JSON string handed back to the model."""
    if isinstance(result, str):
        payload: Any = {"result": result}
    elif isinstance(result, BaseModel):
        payload = result.model_dump(mode="json")
    else:
        payload = result
    # never leak secrets into model history
    payload = redact_payload(payload)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = json.dumps(
            {
                "truncated": True,
                "note": f"result truncated to {max_chars} characters",
                "result": text[:max_chars],
            },
            ensure_ascii=False,
        )
    return text


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration ----------------------------------------------------
    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool '{tool.name}' already registered")
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, params: type[BaseModel], *, risk: Risk = "safe",
             risk_fn: Optional[RiskFn] = None, tags: Optional[list[str]] = None):
        """Decorator: ``@registry.tool("read_csv", "...", ReadCsvParams)``."""

        def decorator(fn: Handler) -> Handler:
            self.register(
                Tool(name=name, description=description, params_model=params, handler=fn,
                     risk=risk, risk_fn=risk_fn, tags=list(tags or []))
            )
            return fn

        return decorator

    # -- lookup ----------------------------------------------------------
    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self, names: Optional[list[str]] = None) -> list[ToolSpec]:
        if names is None:
            return [tool.spec() for tool in self._tools.values()]
        return [self._tools[n].spec() for n in names if n in self._tools]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": tool.risk if tool.risk_fn is None else f"{tool.risk} (dynamic)",
                "tags": tool.tags,
                "parameters": tool.spec().parameters,
            }
            for tool in self._tools.values()
        ]

    # -- execution -------------------------------------------------------
    def execute(self, name: str, arguments: dict[str, Any], ctx: ToolContext,
                *, max_chars: int = 12000) -> tuple[str, bool, int]:
        """Validate + run a tool. Returns ``(json_result, ok, duration_ms)``.

        Never raises for expected failures – errors are serialised so the model
        can react to them.
        """
        started = time.perf_counter()
        tool = self.get(name)
        if tool is None:
            return serialize_result({"error": f"unknown tool '{name}'"}), False, 0
        try:
            params = tool.validate(arguments)
            result = tool.run(params, ctx)
            ok = True
        except ToolError as exc:
            result, ok = {"error": str(exc)}, False
        except Exception as exc:  # unexpected: log with traceback, tell the model briefly
            log.exception("tool %s crashed", name)
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        duration_ms = int((time.perf_counter() - started) * 1000)
        return serialize_result(result, max_chars=max_chars), ok, duration_ms
