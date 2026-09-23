"""Serialisable run state (lets a run pause for approval and resume later)."""
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..providers.base import Message, ToolCall


class RunStatus(str, Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    ERROR = "error"


class Decision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    ALWAYS = "always"      # approve and auto-approve this tool for the rest of the run/session
    DEFER = "defer"        # suspend the run; a client will answer later


class PendingApproval(BaseModel):
    approval_id: str
    tool_name: str
    tool_call: ToolCall
    risk: str = "confirm"
    summary: str = ""
    description: str = ""


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    duration_ms: int = 0
    approved: Optional[bool] = None
    result_preview: str = ""


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str
    user_message: str
    messages: list[Message] = Field(default_factory=list)
    loaded_skills: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    approved_tools: list[str] = Field(default_factory=list)
    step: int = 0
    # loop guards
    tool_call_count: int = 0
    consecutive_failures: int = 0
    seen_signatures: list[str] = Field(default_factory=list)  # ordered for JSON stability
    last_text: str = ""
    repeat_text_count: int = 0
    status: RunStatus = RunStatus.RUNNING
    pending: Optional[PendingApproval] = None
    pending_call_index: int = 0
    final_text: str = ""
    verify_attempts: int = 0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    selection_reason: str = ""
    plan: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    error: Optional[str] = None
    started_at: float = Field(default_factory=lambda: 0.0)

    def last_assistant(self) -> Optional[Message]:
        for message in reversed(self.messages):
            if message.role == "assistant":
                return message
        return None

    def add_usage(self, usage: dict[str, int]) -> None:
        for key, value in usage.items():
            self.usage[key] = self.usage.get(key, 0) + int(value)


class AgentResult(BaseModel):
    """What callers (CLI / API) receive."""

    run_id: str
    session_id: str
    status: RunStatus
    reply: str = ""
    pending: Optional[PendingApproval] = None
    skills_used: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    steps: int = 0
    selection_reason: str = ""
    plan: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    error: Optional[str] = None

    @classmethod
    def from_state(cls, state: RunState) -> "AgentResult":
        return cls(
            run_id=state.run_id,
            session_id=state.session_id,
            status=state.status,
            reply=state.final_text,
            pending=state.pending if state.status == RunStatus.AWAITING_APPROVAL else None,
            skills_used=list(state.loaded_skills),
            tool_calls=list(state.tool_calls),
            steps=state.step,
            selection_reason=state.selection_reason,
            plan=list(state.plan),
            usage=dict(state.usage),
            error=state.error,
        )
