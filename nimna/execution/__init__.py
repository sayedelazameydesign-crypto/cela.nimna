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

__all__ = [
    "Change", "ChangeKind", "FileEntry", "FilesystemDelta",
    "ObservationScope", "ScopeError", "Snapshot", "WorkspaceObserver",
]
