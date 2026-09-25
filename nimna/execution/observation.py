"""Filesystem Observation & Delta — the second Execution Fabric primitive (P1-T2).

A standalone primitive (not a property of ``ShellResult``): Shell now, and
Browser / File tools later, all observe the world through this module.

Contract (owner, P1-T2):

    Snapshot Before → Execute → Snapshot After → Delta Engine
      (CREATED / MODIFIED / DELETED / RENAMED / UNCHANGED) → Evidence

Design rules enforced here:

1. **Content-based, not mtime-based** — every entry carries
   ``path / type / size / mode / sha256``; a modification is proven by a
   different content hash, never by a timestamp.
2. **Bounded by construction** — ``ObservationScope`` (root / include /
   exclude / max_files / max_bytes / max_depth / max_file_bytes). The scope
   root is resolved and *verified inside the workspace* (resolve →
   containment → walk; never ``str.startswith``). The default scope is the
   workspace only — an agent can never make the observer scan ``/``.
3. **No file content in evidence** — hashes + metadata only. Secrets, PII
   and huge files cannot leak through an observation.
4. **Re-verifiable** — ``verify_delta`` re-reads the world and compares
   sha256s: ``Delta + Re-observation = Evidence``, ``Delta ≠ Claim``.
5. **Atomicity is honest** — a failed/timed-out execution still reports its
   side effects; ``NONZERO_EXIT`` with ``a CREATED, b CREATED`` is the truth
   (failure does not roll the world back).
6. **Workspace fingerprint** — ``before_root_hash`` / ``after_root_hash``
   (a Merkle-like digest over sorted entry metadata; the abstraction that
   allows a real Merkle tree later). ``before ≠ after`` is the fast signal,
   the delta is the detail.
7. **Symlinks are recorded, never followed** — a link to ``/etc`` appears as
   a ``symlink`` entry and the walk does not descend; the hash of a symlink
   target is never taken.

Renaming is detected by content: a DELETED and a CREATED entry sharing a
non-``None`` sha256 are reported as one ``RENAMED`` change (``old_path``).
"""
from __future__ import annotations

import enum
import fnmatch
import hashlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


class ScopeError(ValueError):
    """The requested scope escapes the workspace — refused, never clamped."""


class ChangeKind(str, enum.Enum):
    CREATED = "CREATED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"
    UNCHANGED = "UNCHANGED"


@dataclass
class ObservationScope:
    """Bounds of one observation. Defaults observe the workspace only."""
    root: Path | None = None            # None = the workspace root itself
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(default_factory=list)
    max_files: int = 2_000                 # entries (files + dirs + symlinks)
    max_bytes: int = 5_000_000             # total hashing budget (content bytes)
    max_depth: int = 8
    max_file_bytes: int = 1_000_000        # bigger files: presence-only, no hash

    def public_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root) if self.root else ".",
            "include": list(self.include), "exclude": list(self.exclude),
            "max_files": self.max_files, "max_bytes": self.max_bytes,
            "max_depth": self.max_depth, "max_file_bytes": self.max_file_bytes,
        }


def _metadata_dict(entry: FileEntry) -> dict[str, Any]:
    data: dict[str, Any] = {"type": entry.type, "size": entry.size, "mode": entry.mode}
    if entry.sha256 is not None:
        data["sha256"] = entry.sha256
    if entry.note:
        data["note"] = entry.note
    return data


@dataclass
class FileEntry:
    """Metadata-only record of one observed path. Content never lives here."""
    path: str
    type: str                       # file | dir | symlink | inaccessible
    size: int = 0
    mode: str = ""
    sha256: str | None = None    # None for dirs / symlinks / oversize / unhashed
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "type": self.type}
        if self.sha256 is not None:
            data["sha256"] = self.sha256
        if self.size or self.type in {"file", "inaccessible"}:
            data["size"] = self.size
        if self.mode:
            data["mode"] = self.mode
        if self.note:
            data["note"] = self.note
        return data

    def signature(self) -> tuple:
        return (self.type, self.size, self.mode, self.sha256)


@dataclass
class Snapshot:
    snapshot_id: str
    root: str
    created_at: str
    scope: dict[str, Any]
    entries: dict[str, FileEntry]
    root_hash: str                       # Merkle-like digest over sorted entries
    truncated: bool = False              # bounds hit → partial view (marked, never hidden)

    def to_header(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id, "root": self.root,
            "created_at": self.created_at, "root_hash": self.root_hash,
            "entries": len(self.entries), "truncated": self.truncated,
            "scope": self.scope,
        }


@dataclass
class Change:
    kind: ChangeKind
    path: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    old_path: str | None = None       # RENAMED only

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "kind": self.kind.value}
        if self.old_path is not None:
            data["old_path"] = self.old_path
        if self.before is not None:
            data["before"] = self.before
        if self.after is not None:
            data["after"] = self.after
        return data


@dataclass
class FilesystemDelta:
    snapshot_before: str
    snapshot_after: str
    before_root_hash: str
    after_root_hash: str
    changes: list[Change] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_before": self.snapshot_before,
            "snapshot_after": self.snapshot_after,
            "before_root_hash": self.before_root_hash,
            "after_root_hash": self.after_root_hash,
            "changes": [c.to_dict() for c in self.changes],
            "summary": dict(self.summary),
            "truncated": self.truncated,
        }

    @property
    def changed(self) -> bool:
        return self.before_root_hash != self.after_root_hash or bool(self.changes)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FilesystemDelta:
        """Rebuild a delta from its evidence form (audit payload / suite JSON)."""
        changes = [
            Change(ChangeKind(item["kind"]), item["path"],
                   before=item.get("before"), after=item.get("after"),
                   old_path=item.get("old_path"))
            for item in payload.get("changes") or []
        ]
        return cls(
            snapshot_before=payload.get("snapshot_before") or "",
            snapshot_after=payload.get("snapshot_after") or "",
            before_root_hash=payload.get("before_root_hash") or "",
            after_root_hash=payload.get("after_root_hash") or "",
            changes=changes,
            summary=dict(payload.get("summary") or {}),
            truncated=bool(payload.get("truncated")),
        )


def _digest_file(path: Path, max_file_bytes: int, budget: list[int]) -> tuple[str | None, str]:
    """sha256 of file content within the per-file cap and the total budget.

    Returns ``(sha256_or_None, note)``. Hashing is the only content access.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"stat failed: {exc.__class__.__name__}"
    if size > max_file_bytes:
        return None, f"file larger than max_file_bytes ({size} > {max_file_bytes})"
    if budget[0] < size:
        return None, "hash budget exhausted"
    try:
        digest = hashlib.sha256()
        remaining = size
        with path.open("rb") as handle:
            while remaining > 0:
                chunk = handle.read(min(262_144, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        budget[0] -= size
        return "sha256:" + digest.hexdigest(), ""
    except OSError as exc:
        return None, f"read failed: {exc.__class__.__name__}"


def _root_hash(entries: dict[str, FileEntry]) -> str:
    """Merkle-like fingerprint: sha256 over sorted ``path\\0type\\0size\\0mode\\0sha256`` lines."""
    digest = hashlib.sha256()
    for path in sorted(entries):
        entry = entries[path]
        digest.update(f"{path}\0{entry.type}\0{entry.size}\0{entry.mode}\0{entry.sha256 or '-'}\n".encode())
    return "sha256:" + digest.hexdigest()


def resolve_inside_workspace(workspace_root: Path, raw: Path | str) -> Path:
    """The T2 boundary: resolve ``raw`` against ``workspace_root`` and verify
    containment (resolve → containment — never a string prefix check).
    Raises :class:`ScopeError` on escape; returns the resolved absolute path."""
    root = Path(workspace_root).resolve()
    resolved = Path(raw)
    if not resolved.is_absolute():
        resolved = root / resolved
    resolved = resolved.resolve()
    if resolved != root and root not in resolved.parents:
        raise ScopeError(f"path escapes the workspace: {raw} (workspace={root})")
    return resolved


class WorkspaceObserver:
    """Bounded, content-addressed observer over one workspace jail."""

    def __init__(self, workspace_root: Path, scope: ObservationScope | None = None):
        self.workspace_root = workspace_root.resolve()
        scope = scope or ObservationScope()
        raw_root = scope.root or self.workspace_root
        # resolve → containment → walk (never a string prefix check) — T2 boundary
        self.scope = scope
        self.scope.root = resolve_inside_workspace(self.workspace_root, raw_root)

    # -- snapshot -------------------------------------------------------- #
    def snapshot(self) -> Snapshot:
        entries: dict[str, FileEntry] = {}
        truncated = False
        budget = [int(self.scope.max_bytes)]
        root = self.scope.root
        root.mkdir(parents=True, exist_ok=True)

        def on_error(err: OSError) -> None:
            path = getattr(err, "filename", "") or ""
            if path:
                rel = self._rel(path)
                if rel and rel not in entries:
                    entries[rel] = FileEntry(path=rel, type="inaccessible", note="walk denied")

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=on_error):
            rel_dir = self._rel(dirpath)
            depth = 0 if rel_dir == "." else len(PurePosixPath(rel_dir).parts)
            if depth >= self.scope.max_depth:
                dirnames[:] = []  # do not descend past max_depth
            for name in sorted(dirnames):
                if truncated or len(entries) >= self.scope.max_files:
                    truncated = truncated or len(entries) >= self.scope.max_files
                    dirnames[:] = []
                    break
                full = Path(dirpath) / name
                rel = self._rel(str(full))
                if rel is None or not self._included(rel):
                    continue
                if full.is_symlink():
                    entries[rel] = self._symlink_entry(rel, full)
                    dirnames.remove(name)  # record the link, never descend
                    continue
                entries[rel] = self._dir_entry(rel, full)

            for name in sorted(filenames):
                if len(entries) >= self.scope.max_files:
                    truncated = True
                    break
                full = Path(dirpath) / name
                rel = self._rel(str(full))
                if rel is None or not self._included(rel):
                    continue
                if full.is_symlink():
                    entries[rel] = self._symlink_entry(rel, full)
                    continue
                entries[rel] = self._file_entry(rel, full, budget)

        return Snapshot(
            snapshot_id="snap_" + uuid.uuid4().hex[:12],
            root=str(root),
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            scope=self.scope.public_dict(),
            entries=entries,
            root_hash=_root_hash(entries),
            truncated=truncated,
        )

    # -- delta ----------------------------------------------------------- #
    def delta(self, before: Snapshot, after: Snapshot) -> FilesystemDelta:
        changes: list[Change] = []
        unchanged = 0
        for path in sorted(set(before.entries) | set(after.entries)):
            old, new = before.entries.get(path), after.entries.get(path)
            if old is not None and new is not None:
                if old.signature() == new.signature():
                    unchanged += 1
                    continue
                changes.append(Change(ChangeKind.MODIFIED, path,
                                      before=_metadata_dict(old), after=_metadata_dict(new)))
            elif old is not None and new is None:
                changes.append(Change(ChangeKind.DELETED, path, before=_metadata_dict(old)))
            else:
                changes.append(Change(ChangeKind.CREATED, path, after=_metadata_dict(new)))

        # rename detection by content: same non-None sha256 deleted+created
        by_hash: dict[str, Change] = {}
        for change in changes:
            if change.kind is ChangeKind.DELETED and (change.before or {}).get("sha256"):
                by_hash[change.before["sha256"]] = change
        renames: list[Change] = []
        for change in changes:
            sha = (change.after or {}).get("sha256")
            if change.kind is ChangeKind.CREATED and sha and sha in by_hash:
                deleted = by_hash.pop(sha)
                renames.append(Change(ChangeKind.RENAMED, change.path,
                                      before=deleted.before, after=change.after,
                                      old_path=deleted.path))
        renamed_paths = {r.old_path for r in renames} | {r.path for r in renames}
        changes = [c for c in changes if c.path not in renamed_paths] + renames
        changes.sort(key=lambda c: (c.kind.value, c.path))

        summary = {
            "created": sum(1 for c in changes if c.kind is ChangeKind.CREATED),
            "modified": sum(1 for c in changes if c.kind is ChangeKind.MODIFIED),
            "deleted": sum(1 for c in changes if c.kind is ChangeKind.DELETED),
            "renamed": sum(1 for c in changes if c.kind is ChangeKind.RENAMED),
            "unchanged": unchanged,
        }
        return FilesystemDelta(
            snapshot_before=before.snapshot_id, snapshot_after=after.snapshot_id,
            before_root_hash=before.root_hash, after_root_hash=after.root_hash,
            changes=changes, summary=summary,
            truncated=before.truncated or after.truncated,
        )

    # -- re-observation: Delta + Re-observation = Evidence --------------- #
    def verify_delta(self, delta: FilesystemDelta) -> list[dict[str, Any]]:
        """Re-read the world and check every change against reality."""
        results: list[dict[str, Any]] = []
        for change in delta.changes:
            if change.kind is ChangeKind.UNCHANGED:
                continue
            expected = (change.after or {}).get("sha256") if change.kind in {ChangeKind.CREATED, ChangeKind.MODIFIED, ChangeKind.RENAMED} else (change.before or {}).get("sha256")
            check_path = change.path
            target = (self.scope.root / check_path)
            try:  # re-verification never escapes the scope either
                resolved = target.resolve()
                resolved.relative_to(self.scope.root.resolve())
            except (ValueError, OSError):
                results.append({"path": check_path, "kind": change.kind.value,
                                "expected_sha256": expected, "actual_sha256": None,
                                "match": False, "note": "path escapes the observation scope"})
                continue
            exists = target.exists() and not target.is_symlink()
            if change.kind is ChangeKind.DELETED:
                actual = None if not exists else self._hash_now(target)
                match = not exists
                note = "absent as claimed" if match else "still present after DELETED"
            elif not exists:
                actual, match, note = None, False, "claimed present but missing"
            elif expected is None:
                actual, match, note = self._hash_now(target), False, "presence-only change (no hash to verify)"
            else:
                actual = self._hash_now(target)
                match = actual == expected
                note = "hash verified" if match else "hash mismatch"
            results.append({
                "path": check_path, "kind": change.kind.value,
                "expected_sha256": expected, "actual_sha256": actual,
                "match": bool(match), "note": note,
            })
        return results

    # -- internals -------------------------------------------------------- #
    def _rel(self, path: str) -> str | None:
        # Lexical containment only: resolving here would FOLLOW symlinks and
        # silently drop legitimate in-jail symlink entries from the snapshot.
        try:
            rel = Path(path).relative_to(self.workspace_root)
        except (ValueError, OSError):
            return None
        text = str(rel)
        return text if text != "." else "."

    def _included(self, rel: str) -> bool:
        if rel == ".":
            return True
        if any(fnmatch.fnmatch(rel, pattern) for pattern in self.scope.exclude):
            return False
        return any(fnmatch.fnmatch(rel, pattern) for pattern in self.scope.include)

    def _symlink_entry(self, rel: str, full: Path) -> FileEntry:
        try:
            size = full.lstat().st_size
            mode = oct(full.lstat().st_mode & 0o7777)
        except OSError:
            size, mode = 0, ""
        return FileEntry(path=rel, type="symlink", size=size, mode=mode,
                         sha256=None, note="symlink target is never followed or hashed")

    def _dir_entry(self, rel: str, full: Path) -> FileEntry:
        try:
            stat = full.stat()
            return FileEntry(path=rel, type="dir", size=0, mode=oct(stat.st_mode & 0o7777))
        except OSError as exc:
            return FileEntry(path=rel, type="inaccessible", note=exc.__class__.__name__)

    def _file_entry(self, rel: str, full: Path, budget: list[int]) -> FileEntry:
        try:
            stat = full.stat()
            size, mode = stat.st_size, oct(stat.st_mode & 0o7777)
        except OSError as exc:
            return FileEntry(path=rel, type="inaccessible", note=exc.__class__.__name__)
        sha, note = _digest_file(full, self.scope.max_file_bytes, budget)
        return FileEntry(path=rel, type="file", size=size, mode=mode, sha256=sha, note=note)

    def _hash_now(self, path: Path) -> str | None:
        sha, _ = _digest_file(path, self.scope.max_file_bytes, [self.scope.max_bytes])
        return sha
