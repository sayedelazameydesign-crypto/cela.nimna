"""SQLite backup / verify / restore round-trips (offline, temp dirs).

The scripts are exercised as a real operator would run them (subprocess), and
every refusal path is asserted: tampered backups fail verify, unrestorable files
are refused, and restores keep a safety copy of the previous database.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "backup_sqlite.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=str(REPO))


def _seed_db(path: Path) -> None:
    from nimna.memory import MemoryStore

    memory = MemoryStore(str(path))
    memory.ensure_session("sess-1", title="backup probe")
    memory.add_message("sess-1", "user", "hello backup")
    memory.save_memory("backup probe", kind="note")
    memory.idempotency_claim("id1", "k1", "chat", "h", ttl_seconds=3600)
    memory.close()


def test_backup_verify_restore_round_trip(tmp_path):
    db = tmp_path / "nimna.db"
    out = tmp_path / "backups"
    _seed_db(db)

    backup = _run("--db", str(db), "--out-dir", str(out))
    assert backup.returncode == 0, backup.stderr
    files = sorted(out.glob("*.db"))
    manifests = sorted(out.glob("*.manifest.json"))
    assert len(files) == 1 and len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["integrity_ok"] is True
    assert "messages" in manifest["tables"] and "idempotency_keys" in manifest["tables"]

    verify = _run("--verify", str(files[0]))
    assert verify.returncode == 0, verify.stderr

    # destroy the live db, then restore over it
    db.unlink()
    restore = _run("--restore", str(files[0]), "--db", str(db))
    assert restore.returncode == 0, restore.stderr

    from nimna.memory import MemoryStore

    memory = MemoryStore(str(db))
    try:
        assert [m["content"] for m in memory.get_messages("sess-1")] == ["hello backup"]
        assert memory.idempotency_get("id1", "k1") is not None
        assert any(m["content"] == "backup probe" for m in memory.list_memories())
    finally:
        memory.close()


def test_restore_keeps_a_safety_copy(tmp_path):
    db = tmp_path / "nimna.db"
    out = tmp_path / "backups"
    _seed_db(db)
    assert _run("--db", str(db), "--out-dir", str(out)).returncode == 0
    backup_file = next(out.glob("*.db"))
    assert _run("--restore", str(backup_file), "--db", str(db)).returncode == 0
    safety = list(tmp_path.glob("nimna.db.pre-restore-*.bak"))
    assert len(safety) == 1


def test_tampered_backup_fails_verify_and_restore_refuses(tmp_path):
    db = tmp_path / "nimna.db"
    out = tmp_path / "backups"
    _seed_db(db)
    assert _run("--db", str(db), "--out-dir", str(out)).returncode == 0
    backup_file = next(out.glob("*.db"))
    with backup_file.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"TAMPERED!")
    verify = _run("--verify", str(backup_file))
    assert verify.returncode == 1
    assert "CHECKSUM MISMATCH" in verify.stderr
    restore = _run("--restore", str(backup_file), "--db", str(tmp_path / "other.db"))
    assert restore.returncode == 1
    assert not (tmp_path / "other.db").exists(), "unverified bytes must never become the live db"


def test_backup_refuses_missing_database(tmp_path):
    result = _run("--db", str(tmp_path / "nope.db"), "--out-dir", str(tmp_path / "o"))
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_retain_prunes_old_backups(tmp_path):
    import time

    db = tmp_path / "nimna.db"
    out = tmp_path / "backups"
    _seed_db(db)
    for _ in range(3):
        assert _run("--db", str(db), "--out-dir", str(out), "--retain", "2").returncode == 0
        time.sleep(1.05)  # filenames carry 1-second timestamps
    assert len(list(out.glob("*.db"))) == 2
    assert len(list(out.glob("*.manifest.json"))) == 2
