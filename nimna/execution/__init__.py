"""Execution Fabric — standalone execution primitives (observation, and the
ones that follow: verification, recovery). Each primitive is independent of
any single tool so Shell, Browser and File tools can share them."""
from .observation import (
    Change,
    ChangeKind,
    FileEntry,
    FilesystemDelta,
    ObservationScope,
    ScopeError,
    Snapshot,
    WorkspaceObserver,
)
from .verification import CheckResult, DeterministicVerifier, SpecError, Verdict, VerificationReport
from .recovery import (
    Checkpoint,
    CheckpointStore,
    CorruptedCheckpoint,
    IllegalTransition,
    MissingCheckpoint,
    RecoveryAction,
    RecoveryManager,
    RecoveryState,
    RecoveryOutcome,
    UnknownMission,
)
from .tool_registry import (
    AuthorizationDecision,
    DuplicateToolError,
    EvidenceChain,
    IllegalLifecycleTransition,
    InvalidDescriptor,
    InvocationOutcome,
    InvocationStatus,
    LifecycleState,
    PolicyDecision,
    RiskLevel,
    ToolDescriptor,
    ToolNotFound,
    ToolRegistry,
    VersionConflict,
    invoke,
    validate_instance,
)

__all__ = [
    "Change", "ChangeKind", "FileEntry", "FilesystemDelta",
    "ObservationScope", "ScopeError", "Snapshot", "WorkspaceObserver",
    "CheckResult", "DeterministicVerifier", "SpecError", "Verdict", "VerificationReport",
    "Checkpoint", "CheckpointStore", "CorruptedCheckpoint", "IllegalTransition",
    "MissingCheckpoint", "RecoveryAction", "RecoveryManager", "RecoveryState",
    "RecoveryOutcome", "UnknownMission",
    "AuthorizationDecision", "DuplicateToolError", "EvidenceChain",
    "IllegalLifecycleTransition", "InvalidDescriptor", "InvocationOutcome",
    "InvocationStatus", "LifecycleState", "PolicyDecision", "RiskLevel",
    "ToolDescriptor", "ToolNotFound", "ToolRegistry", "VersionConflict",
    "invoke", "validate_instance",
]
