"""Tests for the Checkpoint / Recovery primitive (P1-T4).

Owner's mandatory matrix: normal checkpoint restored · checkpoint after
Execute restored · crash before checkpoint (never claimed) · crash after side
effect (effect detected) · resume completes from last checkpoint · duplicate
resume is refused · corrupted checkpoint ⇒ RECOVERY_ERROR (never PASS) ·
stale checkpoint ⇒ re-observation · completed mission never auto-resumed ·
interrupted execution recovery→resume · timeout checkpoint diagnosable ·
evidence-chain mismatch refused · fingerprint change ⇒ re-observe first ·
authorization expired ⇒ no execution · kill-switch stops recovery.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nimna.execution.observation import WorkspaceObserver
from nimna.execution.recovery import (
    Checkpoint,
    CheckpointStore,
    CorruptedCheckpoint,
    IllegalTransition,
    MissingCheckpoint,
    RecoveryAction,
    RecoveryManager,
    RecoveryState,
)

EVIDENCE_A = "sha256:" + "a" * 64
EVIDENCE_B = "sha256:" + "b" * 64


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    (tmp_path / "checkpoints").mkdir()   # store lives OUTSIDE the observed workspace
    (tmp_path / "work" / "side-effect.txt").write_text("done", encoding="utf-8")
    return tmp_path


def fp(workspace: Path) -> str:
    return WorkspaceObserver(workspace / "work").snapshot().root_hash


def make_manager(ws: Path, **kwargs) -> RecoveryManager:
    return RecoveryManager(CheckpointStore(ws / "checkpoints"), **kwargs)


def attempts_for(step: str, *, status: str = "completed", verified: bool = True) -> dict:
    return {"attempts": [{"step_id": step, "action_hash": "sha256:" + "c" * 64,
                          "status": status, "verified": verified,
                          "evidence_hash": EVIDENCE_A}]}


# --------------------------------------------------------------------------- #
# happy paths (owner rows 1, 2, 5, 10)
# --------------------------------------------------------------------------- #
def test_checkpoint_round_trip_and_restore(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    cp = manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                            evidence_head=EVIDENCE_A,
                            plan_state={"plan": ["step-1", "step-2"], "done": 1},
                            authorization_state={"granted": True, "expires_at": None},
                            execution_state=attempts_for("step-1"))
    assert manager.state("m1") is RecoveryState.CHECKPOINTED

    fresh = make_manager(ws)                    # a NEW manager = a new process
    restored = fresh.restore("m1")              # post-crash adoption
    assert restored.checkpoint_id == cp.checkpoint_id
    assert restored.observation_fingerprint == cp.observation_fingerprint
    assert restored.execution_state["attempts"][0]["step_id"] == "step-1"
    assert fresh.state("m1") is RecoveryState.CHECKPOINTED
    # full field round-trip
    assert restored.to_dict() == cp.to_dict()


def test_resume_after_execute_restores_and_skips_completed(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts_for("step-1"))
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome.action is RecoveryAction.RESUMED and outcome.ok is True
    assert outcome.skip_steps == ["step-1"]     # completed+verified ⇒ never re-run
    assert outcome.recheck_steps == []
    assert manager.state("m1") is RecoveryState.RESUMED


def test_latest_checkpoint_wins_and_resume_completes_from_it(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts_for("step-1"))
    (ws / "work" / "more.txt").write_text("v2", encoding="utf-8")   # world advanced
    cp2 = manager.checkpoint("m1", step_id="step-2", observation_fingerprint=fp(ws),
                             evidence_head=EVIDENCE_B,
                             execution_state=attempts_for("step-2"))
    assert cp2.state_version == 2
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_B)
    assert outcome.action is RecoveryAction.RESUMED
    assert outcome.checkpoint_id == cp2.checkpoint_id
    assert outcome.skip_steps == ["step-2"]


def test_interrupted_execution_recovery_then_resume_history(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts_for("step-1"))
    manager.fail("m1", "runner died mid-mission")            # FAILED
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome.action is RecoveryAction.RESUMED
    transitions = [(h["from"], h["to"]) for h in manager.history("m1")]
    assert ("CHECKPOINTED", "FAILED") in transitions
    assert ("FAILED", "RECOVERING") in transitions            # FAILED → RECOVERING → RESUMED
    assert ("RECOVERING", "RESUMED") in transitions


# --------------------------------------------------------------------------- #
# honest failures (owner rows 3, 7, 12, 14, 15)
# --------------------------------------------------------------------------- #
def test_crash_before_checkpoint_is_never_claimed(ws: Path):
    fresh = make_manager(ws)                    # no checkpoint was ever written
    outcome = fresh.resume("ghost-mission", current_fingerprint=fp(ws))
    assert outcome.action is RecoveryAction.REFUSED
    assert "no checkpoint" in outcome.reason and outcome.ok is False
    with pytest.raises(MissingCheckpoint):
        fresh.restore("ghost-mission")          # restore refuses to invent state too


def test_corrupted_checkpoint_is_recovery_error_never_pass(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A)
    store = CheckpointStore(ws / "checkpoints")
    latest = store.latest("m1")
    path = ws / "checkpoints" / f"{latest.checkpoint_id}.ckpt.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["payload"]["step_id"] = "tampered"     # flip content WITHOUT fixing the checksum
    path.write_text(json.dumps(data), encoding="utf-8")
    outcome = manager.resume("m1", current_fingerprint=fp(ws))
    assert outcome.action is RecoveryAction.RECOVERY_ERROR
    assert "corrupted checkpoint" in outcome.reason and outcome.ok is False
    # a torn (truncated) file is equally unusable
    path.write_text('{"format": "nimna-checkpoint", "payl', encoding="utf-8")
    outcome2 = manager.resume("m1")
    assert outcome2.action is RecoveryAction.RECOVERY_ERROR


def test_evidence_chain_mismatch_refuses_recovery(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts_for("step-1"))
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_B)
    assert outcome.action is RecoveryAction.REFUSED
    assert "evidence chain mismatch" in outcome.reason
    assert manager.state("m1") is RecoveryState.FAILED


def test_authorization_revoked_or_expired_blocks_execution(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A,
                       authorization_state={"granted": False, "expires_at": None})
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome.action is RecoveryAction.REFUSED and "revoked" in outcome.reason

    manager.register("m2")
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    manager.checkpoint("m2", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A,
                       authorization_state={"granted": True, "expires_at": past})
    outcome2 = manager.resume("m2", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome2.action is RecoveryAction.REFUSED and "expired" in outcome2.reason


def test_kill_switch_aborts_recovery(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts_for("step-1"))
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A,
                             kill_switch=lambda: (True, "anomaly detector tripped"))
    assert outcome.action is RecoveryAction.ABORTED
    assert manager.state("m1") is RecoveryState.ABORTED          # terminal: recovery stopped
    transitions = [(h["from"], h["to"]) for h in manager.history("m1")]
    assert ("RECOVERING", "ABORTED") in transitions
    # ABORTED is terminal — no further resume
    outcome2 = manager.resume("m1", current_fingerprint=fp(ws))
    assert outcome2.action is RecoveryAction.REFUSED


# --------------------------------------------------------------------------- #
# fingerprint change + idempotency (owner rows 8, 4, 6, 11)
# --------------------------------------------------------------------------- #
def test_fingerprint_change_requires_reobservation_before_resume(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts_for("step-1"))
    (ws / "work" / "changed-after.txt").write_text("world moved", encoding="utf-8")
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome.action is RecoveryAction.REQUIRES_REOBSERVATION
    assert outcome.ok is None                                    # pending, not a pass
    assert manager.state("m1") is RecoveryState.RECOVERING
    assert outcome.detail["checkpoint_fingerprint"] != outcome.detail["current_fingerprint"]
    # re-observation confirms → resume completes with the skip ledger intact
    confirmed = manager.confirm_reobservation("m1", ok=True, notes="fresh delta re-verified")
    assert confirmed.action is RecoveryAction.RESUMED and confirmed.skip_steps == ["step-1"]
    # a failed re-observation fails recovery instead of resuming
    manager.register("m2")
    manager.checkpoint("m2", step_id="step-1", observation_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    (ws / "work" / "changed-again.txt").write_text("moved", encoding="utf-8")
    manager.resume("m2", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    refused = manager.confirm_reobservation("m2", ok=False, notes="hash mismatch")
    assert refused.action is RecoveryAction.REFUSED
    assert manager.state("m2") is RecoveryState.FAILED


def test_duplicate_resume_is_refused_not_reexecuted(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts_for("step-1"))
    first = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert first.action is RecoveryAction.RESUMED
    second = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert second.action is RecoveryAction.REFUSED               # RESUMED → RECOVERING illegal
    assert "not allowed" in second.reason


def test_timeout_checkpoint_state_is_diagnosable(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    attempts = {"attempts": [
        {"step_id": "step-1", "action_hash": "sha256:" + "d" * 64,
         "status": "attempted", "verified": False, "note": "TIMEOUT after 500ms"},
        {"step_id": "step-2", "action_hash": "sha256:" + "e" * 64,
         "status": "completed", "verified": True, "evidence_hash": EVIDENCE_A},
    ]}
    manager.checkpoint("m1", step_id="step-2", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, execution_state=attempts)
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome.action is RecoveryAction.RESUMED
    assert outcome.skip_steps == ["step-2"]                      # verified work is skipped
    assert outcome.recheck_steps == ["step-1"]                   # the timed-out step is flagged


def test_completed_mission_is_terminal_and_never_auto_resumed(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A)
    manager.complete("m1", "all steps verified")   # CHECKPOINT → NO → COMPLETE (legal)
    assert manager.state("m1") is RecoveryState.COMPLETED
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome.action is RecoveryAction.REFUSED              # COMPLETED → RECOVERING illegal
    with pytest.raises(IllegalTransition):
        manager.transition("m1", RecoveryState.RESUMED, "COMPLETED is terminal")
    # replay is possible ONLY behind the explicit opt-in
    replay_manager = make_manager(ws, allow_replay=True)
    replay_manager._state["m1"] = RecoveryState.COMPLETED
    replay_manager._history.setdefault("m1", [])
    assert replay_manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A).action is RecoveryAction.RESUMED


def test_non_resumable_checkpoint_is_refused(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                       evidence_head=EVIDENCE_A, resumable=False)
    outcome = manager.resume("m1", current_fingerprint=fp(ws), evidence_head=EVIDENCE_A)
    assert outcome.action is RecoveryAction.REFUSED and "non-resumable" in outcome.reason


# --------------------------------------------------------------------------- #
# atomic persistence (owner row 3 adjacent + torn writes)
# --------------------------------------------------------------------------- #
def test_atomic_persistence_torn_temp_files_are_ignored(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    cp = manager.checkpoint("m1", step_id="step-1", observation_fingerprint=fp(ws),
                            evidence_head=EVIDENCE_A)
    # a crash mid-write leaves a temp file behind — it must never be picked up
    torn = ws / "checkpoints" / f".{cp.checkpoint_id}.tmp-deadbeef"
    torn.write_text('{"format": "nimna-checkpoint", "payl', encoding="utf-8")
    store = CheckpointStore(ws / "checkpoints")
    assert store.load(cp.checkpoint_id).checkpoint_id == cp.checkpoint_id
    assert store.latest("m1").checkpoint_id == cp.checkpoint_id
    # a stray non-checkpoint file does not break selection either
    (ws / "checkpoints" / "readme.txt").write_text("not a checkpoint", encoding="utf-8")
    assert store.latest("m1").checkpoint_id == cp.checkpoint_id


def test_structural_validation_blocks_invalid_checkpoints(ws: Path):
    store = CheckpointStore(ws / "checkpoints")
    # syntactically safe id, structurally invalid payload (version 0, no fingerprint)
    bad = Checkpoint(checkpoint_id="ckpt_" + "f" * 24, mission_id="m", step_id="s",
                     state_version=0, created_at="now", observation_fingerprint="")
    with pytest.raises(CorruptedCheckpoint):
        store.save(bad)                        # never persisted
    assert list(Path(ws / "checkpoints").glob("*.ckpt.json")) == []


def test_state_machine_illegal_transitions_matrix(ws: Path):
    manager = make_manager(ws)
    manager.register("m1")
    for illegal in (RecoveryState.RESUMED, RecoveryState.RECOVERING):
        with pytest.raises(IllegalTransition):
            manager.transition("m1", illegal, "RUNNING is not a recovery state")
    manager.checkpoint("m1", step_id="s", observation_fingerprint=fp(ws))
    with pytest.raises(IllegalTransition):
        manager.transition("m1", RecoveryState.RUNNING,
                           "CHECKPOINTED is paused — only resume may re-enter RUNNING")
    manager.abort("m1", "manual stop")
    outcome = manager.resume("m1")             # ABORTED is terminal — refused, not coerced
    assert outcome.action is RecoveryAction.REFUSED and "not allowed" in outcome.reason
    with pytest.raises(IllegalTransition):
        manager.transition("m1", RecoveryState.RUNNING, "ABORTED stays ABORTED")
    with pytest.raises(IllegalTransition):
        manager.register("m1")                 # double registration refused
