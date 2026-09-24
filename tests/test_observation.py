"""Tests for the Filesystem Observation & Delta primitive (P1-T2).

Contract cases covered here: CREATED · MODIFIED · DELETED · UNCHANGED ·
DIRECTORY_CREATED · DIRECTORY_DELETED · RENAME · BINARY_MODIFICATION ·
EMPTY_FILE · LARGE_FILE_BOUND · OUTSIDE_WORKSPACE · SYMLINK ·
PERMISSION_ERROR — plus root-hash fingerprinting and re-verification
(Delta + Re-observation = Evidence). Shell-level side-effect atomicity
(COMMAND_FAILURE/TIMEOUT with side effects) lives in test_shell_tool.py.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from nimna.execution.observation import (
    ChangeKind,
    FilesystemDelta,
    ObservationScope,
    ScopeError,
    WorkspaceObserver,
)

EMPTY_SHA = "sha256:" + hashlib.sha256(b"").hexdigest()


def entry(snapshot, path):
    return snapshot.entries.get(path)


def make_observer(root: Path, scope: ObservationScope | None = None) -> WorkspaceObserver:
    return WorkspaceObserver(root, scope)


# --------------------------------------------------------------------------- #
# basic kinds
# --------------------------------------------------------------------------- #
def test_created_modified_deleted_unchanged(tmp_path: Path):
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "gone.txt").write_text("gone")
    (tmp_path / "chg.txt").write_text("v1")
    obs = make_observer(tmp_path)
    before = obs.snapshot()

    (tmp_path / "new.txt").write_text("new")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "chg.txt").write_text("v2-longer")
    after = obs.snapshot()
    delta = obs.delta(before, after)

    kinds = {c.path: c.kind for c in delta.changes}
    assert kinds["new.txt"] is ChangeKind.CREATED
    assert kinds["gone.txt"] is ChangeKind.DELETED
    assert kinds["chg.txt"] is ChangeKind.MODIFIED
    assert delta.summary["created"] == 1 and delta.summary["deleted"] == 1
    assert delta.summary["modified"] == 1 and delta.summary["unchanged"] >= 1
    # UNCHANGED entries are counted but never listed as changes
    assert "keep.txt" not in kinds and delta.summary["unchanged"] >= 1
    # content-based, not mtime-based: hashes prove the change
    modified = next(c for c in delta.changes if c.path == "chg.txt")
    assert modified.before["sha256"] != modified.after["sha256"]
    assert modified.after["sha256"] == "sha256:" + hashlib.sha256(b"v2-longer").hexdigest()


def test_directory_created_and_deleted(tmp_path: Path):
    obs = make_observer(tmp_path)
    before = obs.snapshot()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1")
    mid = obs.snapshot()
    delta1 = obs.delta(before, mid)
    kinds1 = {c.path: c.kind for c in delta1.changes}
    assert kinds1["pkg"] is ChangeKind.CREATED       # DIRECTORY_CREATED
    assert kinds1["pkg/m.py"] is ChangeKind.CREATED

    import shutil
    shutil.rmtree(tmp_path / "pkg")
    after = obs.snapshot()
    delta2 = obs.delta(mid, after)
    kinds2 = {c.path: c.kind for c in delta2.changes}
    assert kinds2["pkg"] is ChangeKind.DELETED       # DIRECTORY_DELETED
    assert kinds2["pkg/m.py"] is ChangeKind.DELETED


def test_rename_detected_by_content_hash(tmp_path: Path):
    (tmp_path / "a.txt").write_text("same-content")
    obs = make_observer(tmp_path)
    before = obs.snapshot()
    (tmp_path / "a.txt").rename(tmp_path / "b.txt")
    after = obs.snapshot()
    delta = obs.delta(before, after)

    assert delta.summary["renamed"] == 1
    renamed = next(c for c in delta.changes if c.kind is ChangeKind.RENAMED)
    assert renamed.path == "b.txt" and renamed.old_path == "a.txt"
    assert renamed.after["sha256"] == renamed.before["sha256"]


def test_binary_modification_and_empty_file(tmp_path: Path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    obs = make_observer(tmp_path)
    before = obs.snapshot()
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\xfd")
    (tmp_path / "empty.txt").write_bytes(b"")
    delta = obs.delta(before, obs.snapshot())

    kinds = {c.path: c.kind for c in delta.changes}
    assert kinds["blob.bin"] is ChangeKind.MODIFIED      # BINARY_MODIFICATION
    assert kinds["empty.txt"] is ChangeKind.CREATED
    created = next(c for c in delta.changes if c.path == "empty.txt")
    assert created.after["sha256"] == EMPTY_SHA          # EMPTY_FILE has the known hash


def test_large_file_is_presence_only_within_budget(tmp_path: Path):
    scope = ObservationScope(max_file_bytes=100, max_bytes=1_000_000)
    obs = make_observer(tmp_path, scope)
    (tmp_path / "big.txt").write_text("a" * 50)
    before = obs.snapshot()
    assert entry(before, "big.txt").sha256 is not None    # small enough in snapshot 1

    (tmp_path / "big.txt").write_text("b" * 500)          # now over max_file_bytes
    after = obs.snapshot()
    big = entry(after, "big.txt")
    assert big.sha256 is None and "max_file_bytes" in big.note   # LARGE_FILE_BOUND
    assert big.size == 500                                # size is still honest
    delta = obs.delta(before, after)
    modified = next(c for c in delta.changes if c.path == "big.txt")
    assert modified.kind is ChangeKind.MODIFIED           # detected via size/metadata
    assert modified.after.get("sha256") is None           # and honestly unhashed


# --------------------------------------------------------------------------- #
# fingerprinting
# --------------------------------------------------------------------------- #
def test_root_hash_is_deterministic_and_flips_on_change(tmp_path: Path):
    (tmp_path / "x.txt").write_text("x")
    obs = make_observer(tmp_path)
    s1, s2 = obs.snapshot(), obs.snapshot()
    assert s1.root_hash == s2.root_hash and s1.root_hash.startswith("sha256:")
    (tmp_path / "x.txt").write_text("changed")
    s3 = obs.snapshot()
    assert s3.root_hash != s1.root_hash                   # before ≠ after → fast signal


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #
def test_max_files_bound_marks_snapshot_truncated(tmp_path: Path):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text(str(i))
    scope = ObservationScope(max_files=5)
    snap = make_observer(tmp_path, scope).snapshot()
    assert snap.truncated is True and len(snap.entries) <= 5


def test_max_depth_bound_stops_descent(tmp_path: Path):
    deep = tmp_path
    for part in ("l1", "l2", "l3", "l4"):
        deep = deep / part
        deep.mkdir()
    (deep / "deep.txt").write_text("deep")
    scope = ObservationScope(max_depth=2)
    snap = make_observer(tmp_path, scope).snapshot()
    assert "l1/l2" in snap.entries
    assert "l1/l2/l3" not in snap.entries                 # bounded, not crashed
    assert snap.truncated is False                        # depth bound is a scope choice


def test_include_and_exclude_filters(tmp_path: Path):
    (tmp_path / "keep.md").write_text("a")
    (tmp_path / "skip.log").write_text("b")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "also.log").write_text("c")
    scope = ObservationScope(exclude=["*.log"])
    snap = make_observer(tmp_path, scope).snapshot()
    assert "keep.md" in snap.entries and "skip.log" not in snap.entries
    assert "sub/also.log" not in snap.entries and "sub" in snap.entries


# --------------------------------------------------------------------------- #
# jail & symlink safety (resolve → containment → walk, never startswith)
# --------------------------------------------------------------------------- #
def test_scope_outside_workspace_is_refused(tmp_path: Path):
    with pytest.raises(ScopeError):
        make_observer(tmp_path, ObservationScope(root=tmp_path.parent))   # OUTSIDE_WORKSPACE
    with pytest.raises(ScopeError):
        make_observer(tmp_path, ObservationScope(root=Path("/etc")))
    with pytest.raises(ScopeError):
        make_observer(tmp_path, ObservationScope(root=tmp_path / ".." / ".."))
    # a nested scope inside the workspace is fine
    (tmp_path / "sub").mkdir()
    obs = make_observer(tmp_path, ObservationScope(root=tmp_path / "sub"))
    assert obs.scope.root == (tmp_path / "sub").resolve()


def test_symlinks_are_recorded_but_never_followed(tmp_path: Path):
    secret_dir = tmp_path.parent / "observation-outside-secret"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "secret.txt").write_text("TOPSECRET")
    try:
        os.symlink(secret_dir, tmp_path / "leak-dir")     # dir symlink OUT of the jail
        os.symlink(secret_dir / "secret.txt", tmp_path / "leak-file")
        snap = make_observer(tmp_path).snapshot()
        assert entry(snap, "leak-dir").type == "symlink"
        assert entry(snap, "leak-file").type == "symlink"
        assert entry(snap, "leak-file").sha256 is None    # never hashed through the link
        # the walk did NOT descend through the dir symlink
        assert not any(p.startswith("leak-dir/") for p in snap.entries)
        assert "TOPSECRET" not in str(snap.root_hash)     # content can never leak via hash input
    finally:
        import shutil
        shutil.rmtree(secret_dir, ignore_errors=True)


def test_permission_error_is_recorded_without_crashing(tmp_path: Path):
    target = tmp_path / "locked.txt"
    target.write_text("locked")
    mode = target.stat().st_mode
    try:
        os.chmod(target, 0o000)
        snap = make_observer(tmp_path).snapshot()
        locked = entry(snap, "locked.txt")
        assert locked is not None                          # recorded, not skipped silently
        # readable (root) → hashed; unreadable → presence-only with a note. Both honest.
        if os.geteuid() != 0 and locked.sha256 is None:
            assert locked.note
    finally:
        os.chmod(target, mode)


# --------------------------------------------------------------------------- #
# re-verification: Delta + Re-observation = Evidence
# --------------------------------------------------------------------------- #
def test_verify_delta_confirms_created_modified_deleted(tmp_path: Path):
    (tmp_path / "mod.txt").write_text("v1")
    (tmp_path / "del.txt").write_text("bye")
    obs = make_observer(tmp_path)
    before = obs.snapshot()
    (tmp_path / "mod.txt").write_text("v2")
    (tmp_path / "del.txt").unlink()
    (tmp_path / "new.txt").write_text("hello")
    delta = obs.delta(before, obs.snapshot())

    results = {r["path"]: r for r in obs.verify_delta(delta)}
    assert results["new.txt"]["match"] is True and results["new.txt"]["note"] == "hash verified"
    assert results["mod.txt"]["match"] is True
    assert results["del.txt"]["match"] is True and results["del.txt"]["note"] == "absent as claimed"

    # tamper AFTER the fact → re-verification fails (Delta ≠ Claim)
    (tmp_path / "new.txt").write_text("tampered")
    results2 = {r["path"]: r for r in obs.verify_delta(delta)}
    assert results2["new.txt"]["match"] is False and "mismatch" in results2["new.txt"]["note"]


def test_delta_round_trips_through_evidence_payload(tmp_path: Path):
    (tmp_path / "a.txt").write_text("data")
    obs = make_observer(tmp_path)
    before = obs.snapshot()
    (tmp_path / "b.txt").write_text("more")
    delta = obs.delta(before, obs.snapshot())

    payload = delta.to_dict()                       # what the audit stores
    rebuilt = FilesystemDelta.from_payload(payload)
    assert rebuilt.before_root_hash == delta.before_root_hash
    assert [c.to_dict() for c in rebuilt.changes] == [c.to_dict() for c in delta.changes]
    assert {r["path"]: r["match"] for r in obs.verify_delta(rebuilt)}["b.txt"] is True


def test_hash_budget_exhaustion_is_honest(tmp_path: Path):
    scope = ObservationScope(max_bytes=50)          # fits exactly one 40-byte file
    obs = make_observer(tmp_path, scope)
    (tmp_path / "a.txt").write_text("a" * 40)
    (tmp_path / "b.txt").write_text("b" * 40)
    snap = obs.snapshot()
    hashed = [p for p in ("a.txt", "b.txt") if entry(snap, p).sha256 is not None]
    assert len(hashed) == 1                          # one hashed, one honestly skipped
    skipped = entry(snap, "b.txt" if hashed == ["a.txt"] else "a.txt")
    assert "budget" in skipped.note
