"""Checkpoint / Recovery — the fourth Execution Fabric primitive (P1-T4).

Standalone (not a ``ShellResult`` extension): Shell, Browser or File tools —
and the agent loop itself later — all checkpoint and recover through this
module. Recovery is **evidence-based**: a checkpoint carries the workspace
fingerprint (P1-T2), the audit chain head (P1-T1 evidence), an authorization
state, and an idempotency ledger of attempts. ``RESUMED`` is granted only
when the world still matches the evidence; otherwise the honest outcomes are
``REQUIRES_REOBSERVATION`` (world changed → re-observe before resuming),
``REFUSED`` (stale / terminal / unauthorized / duplicate), ``ABORTED``
(kill-switch) or ``RECOVERY_ERROR`` (corrupted checkpoint) — never a
fabricated successful resume of a state that cannot be proven.

Recovery state machine (illegal transitions are refused, never coerced):

    RUNNING → CHECKPOINTED | FAILED | COMPLETED | ABORTED
    CHECKPOINTED → RECOVERING | RESUMED | FAILED | ABORTED
    FAILED → RECOVERING | ABORTED
    RECOVERING → RESUMED | FAILED | ABORTED
    RESUMED → RUNNING | CHECKPOINTED | COMPLETED | FAILED | ABORTED
    COMPLETED → ∅            (terminal; replay only via explicit allow_replay)
    ABORTED → ∅              (terminal — start a new mission id instead)

Atomic persistence: build → validate → persist (tmp file + fsync + atomic
``os.replace`` + directory fsync) → acknowledge. A torn/corrupted file is
detected by an envelope checksum and can never be used for resume.
"""
from __future__ import annotations

import enum
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHECKPOINT_FORMAT = "nimna-checkpoint"
CHECKPOINT_VERSION = 1
MAX_STATE_DICT_BYTES = 1_000_000        # safety cap on the three state blobs
_ID_CHARS = 24


class CorruptedCheckpoint(Exception):
    """Envelope unreadable / wrong format / checksum mismatch — never resumable."""


class MissingCheckpoint(Exception):
    """No checkpoint exists — a crash before checkpointing cannot be claimed."""


class IllegalTransition(ValueError):
    """The requested recovery-state transition is not legal."""


class UnknownMission(KeyError):
    """The mission was never registered with this manager."""


# --------------------------------------------------------------------------- #
# Recovery states
# --------------------------------------------------------------------------- #
class RecoveryState(str, enum.Enum):
    RUNNING = "RUNNING"
    CHECKPOINTED = "CHECKPOINTED"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"
    RESUMED = "RESUMED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


LEGAL_TRANSITIONS: dict[RecoveryState, set[RecoveryState]] = {
    RecoveryState.RUNNING: {
        RecoveryState.CHECKPOINTED, RecoveryState.FAILED,
        RecoveryState.COMPLETED, RecoveryState.ABORTED,
    },
    RecoveryState.CHECKPOINTED: {
        RecoveryState.CHECKPOINTED,  # a newer snapshot supersedes the paused one
        RecoveryState.COMPLETED,     # the owner's loop: CHECKPOINT → FAILURE? NO → COMPLETE
        RecoveryState.RECOVERING, RecoveryState.RESUMED,
        RecoveryState.FAILED, RecoveryState.ABORTED,
    },
    RecoveryState.FAILED: {RecoveryState.RECOVERING, RecoveryState.ABORTED},
    RecoveryState.RECOVERING: {
        RecoveryState.RESUMED, RecoveryState.FAILED, RecoveryState.ABORTED,
    },
    RecoveryState.RESUMED: {
        RecoveryState.RUNNING, RecoveryState.CHECKPOINTED, RecoveryState.COMPLETED,
        RecoveryState.FAILED, RecoveryState.ABORTED,
    },
    RecoveryState.COMPLETED: set(),   # terminal
    RecoveryState.ABORTED: set(),     # terminal
}


class RecoveryAction(str, enum.Enum):
    RESUMED = "RESUMED"
    REQUIRES_REOBSERVATION = "REQUIRES_REOBSERVATION"
    REFUSED = "REFUSED"
    RECOVERY_ERROR = "RECOVERY_ERROR"
    ABORTED = "ABORTED"


# --------------------------------------------------------------------------- #
# Checkpoint model
# --------------------------------------------------------------------------- #
@dataclass
class Checkpoint:
    checkpoint_id: str
    mission_id: str
    step_id: str
    state_version: int
    created_at: str
    plan_state: dict[str, Any] = field(default_factory=dict)
    authorization_state: dict[str, Any] = field(default_factory=dict)
    execution_state: dict[str, Any] = field(default_factory=dict)
    observation_fingerprint: str = ""
    evidence_head: str = ""
    resumable: bool = True
    notes: str = ""

    def validate(self) -> None:
        """Structural validation — run before persistence and after loading."""
        problems: list[str] = []
        for name in ("checkpoint_id", "mission_id", "step_id"):
            if not str(getattr(self, name) or "").strip():
                problems.append(f"{name} is required")
        if not isinstance(self.state_version, int) or self.state_version < 1:
            problems.append("state_version must be a positive integer")
        for name in ("plan_state", "authorization_state", "execution_state"):
            value = getattr(self, name)
            if not isinstance(value, dict):
                problems.append(f"{name} must be a dict")
            elif len(json.dumps(value, ensure_ascii=False).encode()) > MAX_STATE_DICT_BYTES:
                problems.append(f"{name} exceeds the size cap")
        if not str(self.observation_fingerprint or "").strip():
            problems.append("observation_fingerprint is required")
        if not isinstance(self.resumable, bool):
            problems.append("resumable must be a bool")
        if problems:
            raise CorruptedCheckpoint("invalid checkpoint: " + "; ".join(problems))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Checkpoint:
        if not isinstance(data, dict):
            raise CorruptedCheckpoint("checkpoint payload is not an object")
        fields = {f.name for f in __import__("dataclasses").fields(cls)}
        unknown = set(data) - fields
        if unknown:
            raise CorruptedCheckpoint(f"unknown checkpoint fields: {sorted(unknown)}")
        kwargs = {name: data.get(name) for name in fields}
        return cls(**kwargs)


@dataclass
class RecoveryOutcome:
    action: RecoveryAction
    reason: str
    mission_id: str
    state: RecoveryState
    checkpoint_id: str = ""
    skip_steps: list[str] = field(default_factory=list)
    recheck_steps: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool | None:
        """True resumed · False refused/error/aborted · None pending re-observation."""
        if self.action is RecoveryAction.RESUMED:
            return True
        if self.action is RecoveryAction.REQUIRES_REOBSERVATION:
            return None
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value, "reason": self.reason,
            "mission_id": self.mission_id, "state": self.state.value,
            "checkpoint_id": self.checkpoint_id,
            "skip_steps": list(self.skip_steps), "recheck_steps": list(self.recheck_steps),
            "detail": dict(self.detail),
        }


# --------------------------------------------------------------------------- #
# Atomic persistence
# --------------------------------------------------------------------------- #
class CheckpointStore:
    """Filesystem store with atomic, checksummed, fsynced writes.

    Lives *outside* the observed workspace by convention so checkpoints do
    not pollute execution deltas.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, checkpoint_id: str) -> Path:
        safe = "".join(ch for ch in checkpoint_id if ch.isalnum() or ch in "-_")
        if not safe or safe != checkpoint_id:
            raise CorruptedCheckpoint(f"unsafe checkpoint id: {checkpoint_id!r}")
        return self.directory / f"{safe}.ckpt.json"

    def save(self, checkpoint: Checkpoint) -> Path:
        checkpoint.validate()                       # build → validate → persist
        payload = checkpoint.to_dict()
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        envelope = {
            "format": CHECKPOINT_FORMAT,
            "version": CHECKPOINT_VERSION,
            "payload_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            "payload": payload,
        }
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        tmp = self.directory / f".{checkpoint.checkpoint_id}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path(checkpoint.checkpoint_id))   # atomic publish
            try:                                    # fsync the directory entry
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        finally:
            if tmp.exists():                        # a crash mid-write leaves tmp only
                tmp.unlink(missing_ok=True)
        return self._path(checkpoint.checkpoint_id)

    def load(self, checkpoint_id: str) -> Checkpoint:
        path = self._path(checkpoint_id)
        if not path.is_file():
            raise MissingCheckpoint(checkpoint_id)
        return self._read(path)

    def latest(self, mission_id: str) -> Checkpoint:
        """Highest ``state_version`` checkpoint for the mission. Corrupt files
        are skipped for *selection* — but if checkpoint files exist and none is
        readable, the corruption surfaces as CorruptedCheckpoint (RECOVERY_ERROR
        downstream), never disguised as "no checkpoint"."""
        best: Checkpoint | None = None
        files = sorted(self.directory.glob("*.ckpt.json"))
        valid_unmatched = False
        for path in files:
            try:
                checkpoint = self._read(path)
            except CorruptedCheckpoint:
                continue
            if checkpoint.mission_id == mission_id:
                if best is None or (checkpoint.state_version, checkpoint.created_at) > (
                    best.state_version, best.created_at
                ):
                    best = checkpoint
            else:
                valid_unmatched = True
        if best is None:
            if files and not valid_unmatched:
                raise CorruptedCheckpoint(
                    f"{len(files)} checkpoint file(s) exist but none is readable/intact"
                )
            raise MissingCheckpoint(mission_id)
        return best

    def _read(self, path: Path) -> Checkpoint:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptedCheckpoint(f"{path.name}: unreadable ({exc.__class__.__name__})") from None
        if not isinstance(envelope, dict) or envelope.get("format") != CHECKPOINT_FORMAT:
            raise CorruptedCheckpoint(f"{path.name}: not a {CHECKPOINT_FORMAT} envelope")
        body = json.dumps(envelope.get("payload"), ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if not isinstance(envelope.get("payload"), dict) or digest != envelope.get("payload_sha256"):
            raise CorruptedCheckpoint(f"{path.name}: payload checksum mismatch (torn or tampered)")
        checkpoint = Checkpoint.from_dict(envelope["payload"])
        checkpoint.validate()
        return checkpoint


# --------------------------------------------------------------------------- #
# Recovery manager (state machine + evidence-checked resume)
# --------------------------------------------------------------------------- #
class RecoveryManager:
    def __init__(self, store: CheckpointStore, *, now_fn: Callable[[], datetime] | None = None,
                 allow_replay: bool = False):
        self.store = store
        self._now = now_fn or (lambda: datetime.now(UTC))
        self.allow_replay = allow_replay      # explicit opt-in to re-run a COMPLETED mission
        self._state: dict[str, RecoveryState] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    # -- state machine ---------------------------------------------------- #
    def register(self, mission_id: str, *, initial: RecoveryState = RecoveryState.RUNNING) -> RecoveryState:
        if mission_id in self._state:
            raise IllegalTransition(f"{mission_id}: already registered")
        self._state[mission_id] = initial
        self._history[mission_id] = [{"from": initial.value, "to": initial.value,
                                      "reason": "registered", "at": self._now().isoformat(timespec="milliseconds")}]
        return initial

    def state(self, mission_id: str) -> RecoveryState | None:
        return self._state.get(mission_id)

    def history(self, mission_id: str) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._history.get(mission_id, [])]

    def _check_transition(self, mission_id: str, new: RecoveryState, reason: str) -> None:
        current = self._state.get(mission_id)
        if current is None:
            raise UnknownMission(f"{mission_id}: not registered and no adoptable checkpoint")
        allowed = set(LEGAL_TRANSITIONS[current])
        if self.allow_replay and current is RecoveryState.COMPLETED:
            allowed = {RecoveryState.RECOVERING}
        if new not in allowed:
            raise IllegalTransition(
                f"{mission_id}: {current.value} → {new.value} is not allowed ({reason})"
            )

    def _commit_transition(self, mission_id: str, new: RecoveryState, reason: str) -> None:
        previous = self._state[mission_id]
        self._history[mission_id].append({
            "from": previous.value, "to": new.value, "reason": reason[:300],
            "at": self._now().isoformat(timespec="milliseconds"),
        })
        self._state[mission_id] = new

    def transition(self, mission_id: str, new: RecoveryState, reason: str) -> None:
        self._check_transition(mission_id, new, reason)
        self._commit_transition(mission_id, new, reason)

    def complete(self, mission_id: str, reason: str = "mission complete") -> RecoveryState:
        self.transition(mission_id, RecoveryState.COMPLETED, reason)
        return self._state[mission_id]

    def fail(self, mission_id: str, reason: str) -> RecoveryState:
        self.transition(mission_id, RecoveryState.FAILED, reason)
        return self._state[mission_id]

    def abort(self, mission_id: str, reason: str) -> RecoveryState:
        self.transition(mission_id, RecoveryState.ABORTED, reason)
        return self._state[mission_id]

    # -- checkpoint -------------------------------------------------------- #
    def _next_version(self, mission_id: str) -> int:
        try:
            return self.store.latest(mission_id).state_version + 1
        except MissingCheckpoint:
            return 1

    def checkpoint(self, mission_id: str, *, step_id: str,
                   plan_state: dict[str, Any] | None = None,
                   authorization_state: dict[str, Any] | None = None,
                   execution_state: dict[str, Any] | None = None,
                   observation_fingerprint: str,
                   evidence_head: str = "", resumable: bool = True,
                   notes: str = "") -> Checkpoint:
        if mission_id not in self._state:
            raise UnknownMission(f"{mission_id}: register the mission before checkpointing")
        checkpoint = Checkpoint(
            checkpoint_id="ckpt_" + uuid.uuid4().hex[:_ID_CHARS],
            mission_id=mission_id, step_id=str(step_id),
            state_version=self._next_version(mission_id),
            created_at=self._now().isoformat(timespec="milliseconds"),
            plan_state=dict(plan_state or {}),
            authorization_state=dict(authorization_state or {"granted": True, "expires_at": None}),
            execution_state=dict(execution_state or {}),
            observation_fingerprint=str(observation_fingerprint),
            evidence_head=str(evidence_head or ""),
            resumable=bool(resumable), notes=str(notes or ""),
        )
        self._check_transition(mission_id, RecoveryState.CHECKPOINTED,
                               f"checkpoint at {checkpoint.step_id}")
        self.store.save(checkpoint)              # atomic persist before acknowledging
        self._commit_transition(mission_id, RecoveryState.CHECKPOINTED,
                                f"checkpoint {checkpoint.checkpoint_id} at {checkpoint.step_id}")
        return checkpoint

    def restore(self, mission_id: str) -> Checkpoint:
        """Adopt a mission after a crash: state becomes CHECKPOINTED only if a
        valid checkpoint actually exists — a crash *before* checkpointing is
        honestly reported as MissingCheckpoint, never claimed as recoverable."""
        checkpoint = self.store.latest(mission_id)
        if mission_id not in self._state:
            self._state[mission_id] = RecoveryState.CHECKPOINTED
            self._history[mission_id] = [{
                "from": RecoveryState.CHECKPOINTED.value, "to": RecoveryState.CHECKPOINTED.value,
                "reason": f"adopted post-crash from {checkpoint.checkpoint_id}",
                "at": self._now().isoformat(timespec="milliseconds"),
            }]
        return checkpoint

    # -- resume ------------------------------------------------------------- #
    def resume(self, mission_id: str, *, current_fingerprint: str | None = None,
               evidence_head: str | None = None,
               kill_switch: Callable[[], tuple[bool, str]] | None = None) -> RecoveryOutcome:
        """Evidence-checked resume. Order matters: integrity → legality →
        resumable → kill-switch → authorization → evidence chain → fingerprint.
        Every refusal names its reason; nothing is coerced into RESUMED."""

        def _outcome(action: RecoveryAction, reason: str, **kwargs: Any) -> RecoveryOutcome:
            state = self._state.get(mission_id, RecoveryState.FAILED)
            return RecoveryOutcome(action=action, reason=reason, mission_id=mission_id,
                                   state=state, **kwargs)

        # 1) load latest checkpoint — corrupted ⇒ RECOVERY_ERROR, missing ⇒ honest refusal
        try:
            checkpoint = self.store.latest(mission_id)
        except MissingCheckpoint:
            return _outcome(RecoveryAction.REFUSED,
                            "no checkpoint exists for this mission — a crash before "
                            "checkpointing cannot be claimed as recoverable")
        except CorruptedCheckpoint as exc:
            return _outcome(RecoveryAction.RECOVERY_ERROR, f"corrupted checkpoint: {exc}",
                            checkpoint_id="")

        # 2) adopt post-crash state when this manager never saw the mission
        if mission_id not in self._state:
            self._state[mission_id] = RecoveryState.CHECKPOINTED
            self._history[mission_id] = [{
                "from": RecoveryState.CHECKPOINTED.value, "to": RecoveryState.CHECKPOINTED.value,
                "reason": f"adopted post-crash from {checkpoint.checkpoint_id}",
                "at": self._now().isoformat(timespec="milliseconds"),
            }]

        # 3) legality: COMPLETED / ABORTED / RUNNING / RESUMED refuse to re-enter recovery
        try:
            self._check_transition(mission_id, RecoveryState.RECOVERING, "resume requested")
        except IllegalTransition as exc:
            return _outcome(RecoveryAction.REFUSED, str(exc),
                            checkpoint_id=checkpoint.checkpoint_id)
        self._commit_transition(mission_id, RecoveryState.RECOVERING, "resume requested")

        # 4) the checkpoint itself must be marked resumable
        if not checkpoint.resumable:
            self._commit_transition(mission_id, RecoveryState.FAILED, "checkpoint marked non-resumable")
            return _outcome(RecoveryAction.REFUSED, "checkpoint is marked non-resumable",
                            checkpoint_id=checkpoint.checkpoint_id)

        # 5) kill-switch — checked before anything else moves
        if kill_switch is not None:
            try:
                tripped, why = kill_switch()
            except Exception as exc:
                tripped, why = True, f"kill-switch probe failed: {exc.__class__.__name__}"
            if tripped:
                self._commit_transition(mission_id, RecoveryState.ABORTED, f"kill-switch: {why}")
                return _outcome(RecoveryAction.ABORTED, f"kill-switch tripped: {why}",
                                checkpoint_id=checkpoint.checkpoint_id)

        # 6) authorization state — expired or revoked ⇒ no execution
        auth = checkpoint.authorization_state or {}
        if auth.get("granted") is False:
            self._commit_transition(mission_id, RecoveryState.FAILED, "authorization revoked")
            return _outcome(RecoveryAction.REFUSED, "authorization revoked (granted=false)",
                            checkpoint_id=checkpoint.checkpoint_id)
        expires_at = auth.get("expires_at")
        if expires_at:
            try:
                expires = datetime.fromisoformat(str(expires_at))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
            except ValueError:
                self._commit_transition(mission_id, RecoveryState.FAILED, "malformed expires_at")
                return _outcome(RecoveryAction.REFUSED, "authorization expires_at is malformed",
                                checkpoint_id=checkpoint.checkpoint_id)
            if self._now() > expires:
                self._commit_transition(mission_id, RecoveryState.FAILED, f"authorization expired at {expires_at}")
                return _outcome(RecoveryAction.REFUSED,
                                f"authorization expired at {expires_at}",
                                checkpoint_id=checkpoint.checkpoint_id)

        # 7) evidence chain — the audit head must still be the checkpointed head
        if evidence_head is not None and checkpoint.evidence_head and evidence_head != checkpoint.evidence_head:
            self._commit_transition(mission_id, RecoveryState.FAILED, "evidence chain mismatch")
            return _outcome(RecoveryAction.REFUSED,
                            "evidence chain mismatch: workspace evidence moved since the checkpoint "
                            f"({checkpoint.evidence_head[:19]}… != {str(evidence_head)[:19]}…)",
                            checkpoint_id=checkpoint.checkpoint_id,
                            detail={"checkpoint_evidence_head": checkpoint.evidence_head,
                                    "current_evidence_head": str(evidence_head)})

        # 8) filesystem fingerprint — world changed ⇒ re-observe before resuming
        if (current_fingerprint is not None and checkpoint.observation_fingerprint
                and current_fingerprint != checkpoint.observation_fingerprint):
            return _outcome(RecoveryAction.REQUIRES_REOBSERVATION,
                            "filesystem fingerprint changed since the checkpoint — "
                            "re-observe and re-verify before resuming",
                            checkpoint_id=checkpoint.checkpoint_id,
                            detail={"checkpoint_fingerprint": checkpoint.observation_fingerprint,
                                    "current_fingerprint": str(current_fingerprint)})

        # 9) idempotency ledger — completed+verified attempts are skipped, never re-run
        attempts = (checkpoint.execution_state or {}).get("attempts") or []
        skip = sorted({str(a.get("step_id")) for a in attempts
                       if a.get("status") == "completed" and a.get("verified")})
        recheck = sorted({str(a.get("step_id")) for a in attempts} - set(skip))
        self._commit_transition(mission_id, RecoveryState.RESUMED,
                                f"gates passed; resumed from {checkpoint.checkpoint_id}")
        return _outcome(RecoveryAction.RESUMED,
                        f"resumed from {checkpoint.checkpoint_id} (state_version {checkpoint.state_version})",
                        checkpoint_id=checkpoint.checkpoint_id,
                        skip_steps=skip, recheck_steps=recheck)

    def confirm_reobservation(self, mission_id: str, *, ok: bool, notes: str = "") -> RecoveryOutcome:
        """Second half of the fingerprint-changed flow: the caller re-observed
        and re-verified the world; ``ok=True`` completes the resume, otherwise
        the recovery fails honestly."""
        checkpoint_id = ""
        try:
            checkpoint = self.store.latest(mission_id)
            checkpoint_id = checkpoint.checkpoint_id
        except (MissingCheckpoint, CorruptedCheckpoint):
            pass
        if self._state.get(mission_id) is not RecoveryState.RECOVERING:
            return RecoveryOutcome(RecoveryAction.REFUSED,
                                   "no recovery in progress to confirm", mission_id,
                                   self._state.get(mission_id, RecoveryState.FAILED),
                                   checkpoint_id=checkpoint_id)
        if ok:
            attempts = ((checkpoint.execution_state or {}).get("attempts") if checkpoint else []) or []
            skip = sorted({str(a.get("step_id")) for a in attempts
                           if a.get("status") == "completed" and a.get("verified")})
            recheck = sorted({str(a.get("step_id")) for a in attempts} - set(skip))
            self._commit_transition(mission_id, RecoveryState.RESUMED,
                                    f"re-observation confirmed: {notes or 'ok'}")
            return RecoveryOutcome(RecoveryAction.RESUMED,
                                   f"resumed after re-observation ({notes or 'ok'})", mission_id,
                                   RecoveryState.RESUMED, checkpoint_id=checkpoint_id,
                                   skip_steps=skip, recheck_steps=recheck)
        self._commit_transition(mission_id, RecoveryState.FAILED,
                                f"re-observation failed: {notes or 'unverified'}")
        return RecoveryOutcome(RecoveryAction.REFUSED,
                               "re-observation did not verify the checkpointed state", mission_id,
                               RecoveryState.FAILED, checkpoint_id=checkpoint_id)
