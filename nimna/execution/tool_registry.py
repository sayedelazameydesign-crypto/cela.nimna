"""Tool Registry primitive (P1-T5) — discovery, lifecycle and gated invocation.

Ticket P1-T5 in ``docs/architecture/agent-platform-audit.md``: tools become
discoverable, governable Capabilities — not a dict of callables. Separation of
responsibilities is the contract (no God Object):

* **Registry**       — "what tools exist?" (register / discover / lifecycle)
* **Capability**     — "what does this tool require?" (injected resolver)
* **Policy**         — "is this capability allowed?" (injected callable)
* **Authorization**  — "is this call authorized now?" (injected callable)
* **Executor**       — the descriptor's handler (Observe/Verify stay inside the
  tool's own evidence, exactly like the shell primitive — not here)
* **Evidence**       — a hash-chained record of EVERY invocation outcome
  (refusals included), digests only — no raw arguments, no secrets.

Invocation gate order (all deterministic, all refusing closed):
registry lookup → lifecycle → input schema → capability → policy →
authorization → execute → output schema → evidence. An unvalidated input never
reaches the handler; a handler exception or an output-schema violation is an
honest ``ok=False`` — it can never surface as PASS.
"""
from __future__ import annotations

import enum
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Any, Callable, Iterable, Mapping, Optional

__all__ = [
    "RiskLevel", "LifecycleState", "LIFECYCLE_TRANSITIONS",
    "ToolDescriptor", "ToolRegistry",
    "PolicyDecision", "AuthorizationDecision",
    "InvocationStatus", "InvocationOutcome", "EvidenceChain",
    "invoke", "validate_instance",
    "InvalidDescriptor", "DuplicateToolError", "ToolNotFound",
    "VersionConflict", "IllegalLifecycleTransition",
]

# --------------------------------------------------------------------------- #
# constants (bounded, deterministic)
# --------------------------------------------------------------------------- #
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")   # namespaced ids: sandbox.command
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

MAX_DESCRIPTOR_BYTES = 65_536      # canonical-JSON bound per schema
MAX_ARGUMENTS_BYTES = 262_144      # digest input bound
MAX_EVIDENCE_ENTRIES = 1_000       # bounded chain (older entries pruned, anchor kept)

SIDE_EFFECT_VOCABULARY = frozenset({"none", "filesystem", "process", "network", "state", "external"})

_SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "items", "enum",
    "additionalProperties", "minItems", "maxItems", "minLength", "maxLength",
})
_TYPE_NAMES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})

_EVIDENCE_GENESIS = "sha256:" + hashlib.sha256(b"nimna-tool-evidence-v1").hexdigest()


class InvalidDescriptor(Exception):
    """Descriptor fails structural/schema validation — never registered."""


class DuplicateToolError(Exception):
    """A tool with this id is already registered (duplicate protection)."""


class ToolNotFound(Exception):
    """Unknown tool_id — discovery returns NOT_FOUND, never a guess."""


class VersionConflict(Exception):
    """replace() with a version that is not a strictly greater semver bump."""


class IllegalLifecycleTransition(Exception):
    """A lifecycle move outside LIFECYCLE_TRANSITIONS (e.g. REVOKED → ENABLED)."""


# --------------------------------------------------------------------------- #
# schema subset — deterministic stdlib validator
# --------------------------------------------------------------------------- #
def validate_schema_definition(schema: Any, *, where: str) -> None:
    """Reject malformed schemas at registration time (no silent ignores)."""
    def _check(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            raise InvalidDescriptor(f"{where}: {path} must be an object schema")
        unknown = set(node) - _SCHEMA_KEYS
        if unknown:
            raise InvalidDescriptor(f"{where}: {path} has unsupported keys {sorted(unknown)}")
        size = len(json.dumps(node, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if size > MAX_DESCRIPTOR_BYTES:
            raise InvalidDescriptor(f"{where}: {path} exceeds {MAX_DESCRIPTOR_BYTES} bytes")
        type_ = node.get("type")
        if type_ is not None and type_ not in _TYPE_NAMES:
            raise InvalidDescriptor(f"{where}: {path} has unknown type {type_!r}")
        properties = node.get("properties")
        if properties is not None:
            if not isinstance(properties, dict):
                raise InvalidDescriptor(f"{where}: {path}.properties must be a dict")
            for key, sub in properties.items():
                _check(sub, f"{path}.{key}")
        required = node.get("required")
        if required is not None:
            if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
                raise InvalidDescriptor(f"{where}: {path}.required must be a list of names")
            if properties is not None:
                for key in required:
                    if key not in properties:
                        raise InvalidDescriptor(f"{where}: {path}.required names unknown property {key!r}")
        items = node.get("items")
        if items is not None:
            _check(items, f"{path}[]")
        additional = node.get("additionalProperties")
        if additional is not None and additional is not False and not isinstance(additional, bool):
            _check(additional, f"{path}.*")
        enum_ = node.get("enum")
        if enum_ is not None and (not isinstance(enum_, list) or not enum_):
            raise InvalidDescriptor(f"{where}: {path}.enum must be a non-empty list")
        for bound in ("minItems", "maxItems", "minLength", "maxLength"):
            if bound in node and (not isinstance(node[bound], int) or isinstance(node[bound], bool)
                                  or node[bound] < 0):
                raise InvalidDescriptor(f"{where}: {path}.{bound} must be a non-negative integer")

    _check(schema, "$")


def validate_instance(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Validate a value against the schema subset; returns violation paths."""
    problems: list[str] = []
    type_ = schema.get("type")
    if type_ is not None:
        ok: bool
        if type_ == "object":
            ok = isinstance(value, dict)
        elif type_ == "array":
            ok = isinstance(value, list)
        elif type_ == "string":
            ok = isinstance(value, str)
        elif type_ == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif type_ == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif type_ == "boolean":
            ok = isinstance(value, bool)
        else:  # "null"
            ok = value is None
        if not ok:
            problems.append(f"{path}: expected {type_}, got {type(value).__name__}")
            return problems
    enum_ = schema.get("enum")
    if enum_ is not None and value not in enum_:
        problems.append(f"{path}: {value!r} not in enum")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{path}: missing required property {key!r}")
        additional = schema.get("additionalProperties")
        for key, sub in value.items():
            if key in properties:
                problems += validate_instance(sub, properties[key], f"{path}.{key}")
            elif additional is False:
                problems.append(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, dict):
                problems += validate_instance(sub, additional, f"{path}.{key}")
    if isinstance(value, list):
        items = schema.get("items")
        min_items, max_items = schema.get("minItems"), schema.get("maxItems")
        if min_items is not None and len(value) < min_items:
            problems.append(f"{path}: fewer than minItems={min_items}")
        if max_items is not None and len(value) > max_items:
            problems.append(f"{path}: more than maxItems={max_items}")
        if items is not None:
            for index, sub in enumerate(value):
                problems += validate_instance(sub, items, f"{path}[{index}]")
    if isinstance(value, str):
        min_len, max_len = schema.get("minLength"), schema.get("maxLength")
        if min_len is not None and len(value) < min_len:
            problems.append(f"{path}: shorter than minLength={min_len}")
        if max_len is not None and len(value) > max_len:
            problems.append(f"{path}: longer than maxLength={max_len}")
    return problems


# --------------------------------------------------------------------------- #
# descriptor + lifecycle
# --------------------------------------------------------------------------- #
class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class LifecycleState(str, enum.Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    DEPRECATED = "DEPRECATED"
    REVOKED = "REVOKED"


# explicit, closed transition table — REVOKED is terminal
LIFECYCLE_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.ENABLED: frozenset({LifecycleState.DISABLED, LifecycleState.DEPRECATED,
                                       LifecycleState.REVOKED}),
    LifecycleState.DISABLED: frozenset({LifecycleState.ENABLED, LifecycleState.DEPRECATED,
                                        LifecycleState.REVOKED}),
    LifecycleState.DEPRECATED: frozenset({LifecycleState.REVOKED}),
    LifecycleState.REVOKED: frozenset(),
}


@dataclass(frozen=True)
class ToolDescriptor:
    """Declarative tool contract: identity, schemas, governance metadata, handler."""
    tool_id: str
    version: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    capabilities: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ("none",)
    risk_level: str = "LOW"
    availability: str = "ENABLED"
    handler: Optional[Callable[..., Any]] = None

    def validate(self) -> None:
        if not isinstance(self.tool_id, str) or not TOOL_ID_RE.match(self.tool_id or ""):
            raise InvalidDescriptor(f"tool_id {self.tool_id!r} must match {TOOL_ID_RE.pattern}")
        if not isinstance(self.version, str) or not VERSION_RE.match(self.version or ""):
            raise InvalidDescriptor(f"version {self.version!r} must be MAJOR.MINOR.PATCH")
        validate_schema_definition(self.input_schema, where=f"{self.tool_id}.input_schema")
        validate_schema_definition(self.output_schema, where=f"{self.tool_id}.output_schema")
        if not isinstance(self.capabilities, tuple) or not all(
            isinstance(cap, str) and CAPABILITY_RE.match(cap) for cap in self.capabilities
        ):
            raise InvalidDescriptor(f"{self.tool_id}: capabilities must be a tuple of capability names")
        if not isinstance(self.side_effects, tuple) or not all(
            isinstance(se, str) and se in SIDE_EFFECT_VOCABULARY for se in self.side_effects
        ):
            raise InvalidDescriptor(
                f"{self.tool_id}: side_effects must be from {sorted(SIDE_EFFECT_VOCABULARY)}")
        try:
            RiskLevel(self.risk_level)
        except ValueError:
            raise InvalidDescriptor(f"{self.tool_id}: risk_level {self.risk_level!r} unknown") from None
        try:
            LifecycleState(self.availability)
        except ValueError:
            raise InvalidDescriptor(f"{self.tool_id}: availability {self.availability!r} unknown") from None
        if not callable(self.handler):
            raise InvalidDescriptor(f"{self.tool_id}: handler must be callable")

    @property
    def lifecycle(self) -> LifecycleState:
        return LifecycleState(self.availability)

    @property
    def version_key(self) -> tuple[int, int, int]:
        return tuple(int(part) for part in self.version.split("."))  # type: ignore[return-value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id, "version": self.version,
            "input_schema": dict(self.input_schema), "output_schema": dict(self.output_schema),
            "capabilities": list(self.capabilities), "side_effects": list(self.side_effects),
            "risk_level": self.risk_level, "availability": self.availability,
            "handler": getattr(self.handler, "__qualname__", repr(self.handler)),
        }


class ToolRegistry:
    """What tools exist? Registration + discovery + lifecycle — nothing more."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> ToolDescriptor:
        descriptor.validate()
        if descriptor.tool_id in self._tools:      # duplicate protection (M1)
            raise DuplicateToolError(
                f"{descriptor.tool_id}: already registered (use replace() for a version bump)")
        self._tools[descriptor.tool_id] = descriptor
        return descriptor

    def unregister(self, tool_id: str) -> ToolDescriptor:
        if tool_id not in self._tools:
            raise ToolNotFound(f"{tool_id}: not registered")
        return self._tools.pop(tool_id)

    def replace(self, descriptor: ToolDescriptor) -> ToolDescriptor:
        descriptor.validate()
        current = self._tools.get(descriptor.tool_id)
        if current is None:
            raise ToolNotFound(f"{descriptor.tool_id}: replace() needs an existing tool")
        if current.lifecycle is LifecycleState.REVOKED:
            raise IllegalLifecycleTransition(
                f"{descriptor.tool_id}: REVOKED is terminal — register a new tool_id instead")
        if descriptor.version_key <= current.version_key:
            raise VersionConflict(
                f"{descriptor.tool_id}: replace needs a strictly greater version "
                f"(current {current.version}, got {descriptor.version})")
        self._tools[descriptor.tool_id] = descriptor
        return descriptor

    def get(self, tool_id: str) -> ToolDescriptor:
        tool = self._tools.get(tool_id)
        if tool is None:
            raise ToolNotFound(f"{tool_id}: NOT_FOUND")
        return tool

    def list(self) -> list[ToolDescriptor]:
        """Deterministic discovery — always sorted by tool_id."""
        return [self._tools[tool_id] for tool_id in sorted(self._tools)]

    def tools_requiring(self, capability: str) -> list[str]:
        """Capability query: which registered tools declare this capability?"""
        return sorted(tool.tool_id for tool in self._tools.values()
                      if capability in tool.capabilities)

    def state(self, tool_id: str) -> LifecycleState:
        return self.get(tool_id).lifecycle

    def set_availability(self, tool_id: str, state: LifecycleState | str) -> LifecycleState:
        target = LifecycleState(state)
        current = self.state(tool_id)
        if current is target:
            return current
        if target not in LIFECYCLE_TRANSITIONS[current]:
            raise IllegalLifecycleTransition(
                f"{tool_id}: {current.value} → {target.value} is not a legal lifecycle move")
        updated = _dc_replace(self._tools[tool_id], availability=target.value)
        self._tools[tool_id] = updated
        return target

    def __len__(self) -> int:
        return len(self._tools)


# --------------------------------------------------------------------------- #
# gated invocation + evidence
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class AuthorizationDecision:
    granted: bool
    reason: str = ""


class InvocationStatus(str, enum.Enum):
    NOT_FOUND = "NOT_FOUND"
    DISABLED = "DISABLED"
    REVOKED = "REVOKED"
    INPUT_INVALID = "INPUT_INVALID"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    POLICY_DENIED = "POLICY_DENIED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    EXECUTED = "EXECUTED"
    HANDLER_ERROR = "HANDLER_ERROR"
    OUTPUT_VIOLATION = "OUTPUT_VIOLATION"


_GATE = {
    InvocationStatus.NOT_FOUND: "registry",
    InvocationStatus.DISABLED: "lifecycle",
    InvocationStatus.REVOKED: "lifecycle",
    InvocationStatus.INPUT_INVALID: "input-schema",
    InvocationStatus.CAPABILITY_DENIED: "capability",
    InvocationStatus.POLICY_DENIED: "policy",
    InvocationStatus.AUTHORIZATION_DENIED: "authorization",
    InvocationStatus.EXECUTED: "executor",
    InvocationStatus.HANDLER_ERROR: "executor",
    InvocationStatus.OUTPUT_VIOLATION: "output-schema",
}


@dataclass
class InvocationOutcome:
    status: InvocationStatus
    ok: bool
    executed: bool                    # did the handler run? (side-effect honesty)
    reason: str
    gate: str
    tool_id: str
    result: Any = None
    evidence_event: Optional[dict[str, Any]] = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "ok": self.ok, "executed": self.executed,
                "reason": self.reason, "gate": self.gate, "tool_id": self.tool_id,
                "duration_ms": self.duration_ms,
                "evidence_hash": (self.evidence_event or {}).get("hash", "")}


def _canonical_digest(payload: Any, limit: int) -> str:
    try:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return "unserializable"
    if len(body) > limit:
        return "oversized"
    return "sha256:" + hashlib.sha256(body).hexdigest()


class EvidenceChain:
    """Hash-chained invocation evidence (digests only — never raw arguments)."""

    def __init__(self, *, max_entries: int = MAX_EVIDENCE_ENTRIES) -> None:
        self._max = max_entries
        self._entries: list[dict[str, Any]] = []
        self._tail = _EVIDENCE_GENESIS
        self._seq = 0
        self._pruned = 0

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self._seq += 1
        entry = {"seq": self._seq, "prev_hash": self._tail, **event}
        body = json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")
        entry["hash"] = "sha256:" + hashlib.sha256(body).hexdigest()
        self._entries.append(entry)
        self._tail = entry["hash"]
        while len(self._entries) > self._max:
            self._entries.pop(0)
            self._pruned += 1
        return entry

    @property
    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._entries)

    @property
    def tail(self) -> str:
        return self._tail

    @property
    def pruned(self) -> int:
        return self._pruned

    def __len__(self) -> int:
        return len(self._entries)

    def verify(self) -> bool:
        """Recompute the retained chain — tampering breaks the links."""
        prev = _EVIDENCE_GENESIS
        for entry in self._entries:
            body = json.dumps({k: v for k, v in entry.items() if k != "hash"},
                              ensure_ascii=False, sort_keys=True).encode("utf-8")
            expected = "sha256:" + hashlib.sha256(body).hexdigest()
            if entry.get("prev_hash") != prev or entry.get("hash") != expected:
                return False
            prev = entry["hash"]
        return True


def invoke(
    registry: ToolRegistry,
    tool_id: str,
    arguments: Any,
    *,
    granted_capabilities: Iterable[str] = frozenset(),
    capability_resolver: Optional[Callable[[ToolDescriptor], Iterable[str]]] = None,
    policy: Optional[Callable[[ToolDescriptor, Mapping[str, Any]], PolicyDecision]] = None,
    authorizer: Optional[Callable[[ToolDescriptor, Mapping[str, Any]], AuthorizationDecision]] = None,
    evidence: Optional[EvidenceChain] = None,
) -> InvocationOutcome:
    """Run the full gate pipeline. Refusals close early; the handler only ever
    sees arguments that passed input-schema, capability, policy and authorization."""
    started = time.perf_counter()
    granted = frozenset(granted_capabilities)

    def _event(status: InvocationStatus, reason: str, *, executed: bool = False,
               result: Any = None, deprecated: bool = False,
               extra: Optional[Mapping[str, Any]] = None) -> InvocationOutcome:
        event: dict[str, Any] = {
            "invocation_id": "inv_" + hashlib.sha256(
                f"{tool_id}:{time.perf_counter_ns()}:{status.value}".encode()).hexdigest()[:12],
            "tool_id": tool_id,
            "decision": status.value,
            "gate": _GATE[status],
            "reason": reason[:300],
            "executed": executed,
            "arguments_digest": _canonical_digest(arguments, MAX_ARGUMENTS_BYTES),
            "arguments_keys": sorted(arguments) if isinstance(arguments, dict) else [],
            "granted_capabilities": sorted(granted),
            "result_digest": _canonical_digest(result, MAX_ARGUMENTS_BYTES) if executed else "",
            "deprecated": deprecated,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            **(extra or {}),
        }
        stored = evidence.append(event) if evidence is not None else dict(event)
        return InvocationOutcome(status=status, ok=status is InvocationStatus.EXECUTED,
                                 executed=executed, reason=reason, gate=_GATE[status],
                                 tool_id=tool_id, result=result,
                                 evidence_event=stored,
                                 duration_ms=event["duration_ms"])

    # 1) registry lookup
    try:
        descriptor = registry.get(tool_id)
    except ToolNotFound:
        return _event(InvocationStatus.NOT_FOUND, f"{tool_id}: no such tool")

    # 2) lifecycle (M5/M6)
    lifecycle = descriptor.lifecycle
    if lifecycle is LifecycleState.DISABLED:
        return _event(InvocationStatus.DISABLED, f"{tool_id}: tool is DISABLED")
    if lifecycle is LifecycleState.REVOKED:
        return _event(InvocationStatus.REVOKED, f"{tool_id}: tool is REVOKED")
    deprecated = lifecycle is LifecycleState.DEPRECATED

    # 3) input schema — invalid arguments never reach the handler (M7)
    problems = validate_instance(arguments, descriptor.input_schema)
    if problems:
        return _event(InvocationStatus.INPUT_INVALID,
                      f"{tool_id}: input schema violations: {'; '.join(problems[:4])}")

    # 4) capability check (M2) — a resolver failure is fail-closed, never fail-open
    try:
        required = set(capability_resolver(descriptor)) if capability_resolver else set(descriptor.capabilities)
    except ValueError as exc:
        return _event(InvocationStatus.CAPABILITY_DENIED,
                      f"{tool_id}: capability resolution refused: {str(exc)[:150]}",
                      deprecated=deprecated)
    missing = sorted(required - granted)
    if missing:
        return _event(InvocationStatus.CAPABILITY_DENIED,
                      f"{tool_id}: missing capabilities {missing} (granted {sorted(granted)})",
                      deprecated=deprecated)

    # 5) policy check (M3) — default deny
    decision = (policy(descriptor, arguments) if policy
                else PolicyDecision(False, "no policy wired (default-deny)"))
    if not decision.allowed:
        return _event(InvocationStatus.POLICY_DENIED,
                      f"{tool_id}: policy denied: {decision.reason or 'not allowed'}",
                      deprecated=deprecated)

    # 6) authorization check (M4) — default deny
    authorization = (authorizer(descriptor, arguments) if authorizer
                     else AuthorizationDecision(False, "no authorizer wired (default-deny)"))
    if not authorization.granted:
        return _event(InvocationStatus.AUTHORIZATION_DENIED,
                      f"{tool_id}: not authorized: {authorization.reason or 'denied'}",
                      deprecated=deprecated)

    # 7) execute — a handler exception is an honest error, never PASS
    try:
        result = descriptor.handler(dict(arguments))  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 — the boundary records, never propagates
        return _event(InvocationStatus.HANDLER_ERROR,
                      f"{tool_id}: handler raised {exc.__class__.__name__}: {str(exc)[:150]}",
                      executed=True, deprecated=deprecated)

    # 8) output schema — a violation is ok=False even though side effects happened (M8)
    violations = validate_instance(result, descriptor.output_schema)
    if violations:
        return _event(InvocationStatus.OUTPUT_VIOLATION,
                      f"{tool_id}: output schema violations: {'; '.join(violations[:4])}",
                      executed=True, result=result, deprecated=deprecated)

    return _event(InvocationStatus.EXECUTED, f"{tool_id}: executed", executed=True,
                  result=result, deprecated=deprecated,
                  extra={"policy_reason": str(getattr(decision, "reason", ""))[:200],
                         "authorization_reason": str(getattr(authorization, "reason", ""))[:200]})
