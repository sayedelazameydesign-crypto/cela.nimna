"""Execution Fabric — standalone execution primitives (observation, and the
ones that follow: verification, recovery). Each primitive is independent of
any single tool so Shell, Browser and File tools can share them."""
from .gateway import ExecutionGateway, GatewayOutcome, InvocationContext
from .observation import (
    Change,
    ChangeKind,
    FileEntry,
    FilesystemDelta,
    ObservationScope,
    ScopeError,
    Snapshot,
    WorkspaceObserver,
    resolve_inside_workspace,
)
from .policy import (
    AuthorizationGrant,
    Authorizer,
    CapabilityCatalog,
    Effect,
    Policy,
    PolicyError,
    PolicyInput,
    PolicyOutcome,
    PolicyRule,
    WorkspaceBoundary,
    adjudicate,
    t5_authorizer_adapter,
    t5_capability_resolver,
    t5_policy_adapter,
)
from .recovery import (
    Checkpoint,
    CheckpointStore,
    CorruptedCheckpoint,
    IllegalTransition,
    MissingCheckpoint,
    RecoveryAction,
    RecoveryManager,
    RecoveryOutcome,
    RecoveryState,
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
    PolicyGateDecision,
    RiskLevel,
    ToolDescriptor,
    ToolNotFound,
    ToolRegistry,
    VersionConflict,
    invoke,
    validate_instance,
)
from .verification import CheckResult, DeterministicVerifier, SpecError, Verdict, VerificationReport

__all__ = [
    "Change", "ChangeKind", "FileEntry", "FilesystemDelta",
    "ObservationScope", "ScopeError", "Snapshot", "WorkspaceObserver",
    "resolve_inside_workspace",
    "CheckResult", "DeterministicVerifier", "SpecError", "Verdict", "VerificationReport",
    "Checkpoint", "CheckpointStore", "CorruptedCheckpoint", "IllegalTransition",
    "MissingCheckpoint", "RecoveryAction", "RecoveryManager", "RecoveryState",
    "RecoveryOutcome", "UnknownMission",
    "AuthorizationDecision", "DuplicateToolError", "EvidenceChain",
    "IllegalLifecycleTransition", "InvalidDescriptor", "InvocationOutcome",
    "InvocationStatus", "LifecycleState", "PolicyGateDecision", "RiskLevel",
    "ToolDescriptor", "ToolNotFound", "ToolRegistry", "VersionConflict",
    "invoke", "validate_instance",
    "ExecutionGateway", "GatewayOutcome", "InvocationContext",
    "AuthorizationGrant", "Authorizer", "CapabilityCatalog", "Effect",
    "Policy", "PolicyError", "PolicyInput", "PolicyOutcome", "PolicyRule",
    "WorkspaceBoundary", "adjudicate", "t5_authorizer_adapter",
    "t5_capability_resolver", "t5_policy_adapter",
]
