#!/usr/bin/env python3
"""SQLite backup / verify / restore for the Nimna canonical store (offline).

Why this file: the agent's source of truth is a single SQLite file
(``data/nimna.db``).  Until/unless that migrates to Postgres, disaster
recovery is: copy the file with the SQLite backup API, checksum it, and prove
the copy opens with ``integrity_check=ok``.  Every step here verifies instead
of assuming — see ``docs/RESILIENCE.md`` (RPO/RTO).

    python scripts/backup_sqlite.py --db data/nimna.db --out-dir data/backups
    python scripts/backup_sqlite.py --verify data/backups/2026....db
    python scripts/backup_sqlite.py --restore data/backups/2026....db --db data/nimna.db

Exit codes: 0 = ok, 1 = operation failed, 64 = usage error.  Nothing is ever
half-written: backups land via temp-file + atomic rename, restores keep a
``.pre-restore-<ts>.bak`` safety copy of the previous database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_db(path: Path) -> dict:
    """Open read-only and return tables + integrity verdict. Raises on failure."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check;")]
        counts = {}
        for table in tables:
            try:
                counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                counts[table] = -1
    finally:
        conn.close()
    return {"tables": tables, "row_counts": counts,
            "integrity_ok": integrity == ["ok"], "integrity": integrity[:5]}


def cmd_backup(db: Path, out_dir: Path, retain: int) -> int:
    if not db.is_file():
        print(f"backup: database not found: {db}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_ts()
    final_db = out_dir / f"{stamp}.db"
    final_manifest = out_dir / f"{stamp}.manifest.json"
    if final_db.exists() or final_manifest.exists():
        print(f"backup: refusing to overwrite existing {stamp} files", file=sys.stderr)
        return 1
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="nimna-backup-", suffix=".db", dir=str(out_dir))
    tmp_path = Path(tmp_name)
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(str(tmp_path), timeout=30)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        report = inspect_db(tmp_path)
        if not report["integrity_ok"]:
            print(f"backup: copy failed integrity_check: {report['integrity']}", file=sys.stderr)
            return 1
        checksum = _sha256(tmp_path)
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": str(db),
            "file": final_db.name,
            "sha256": checksum,
            "bytes": tmp_path.stat().st_size,
            **report,
        }
        tmp_manifest = tmp_path.with_suffix(".manifest.json")
        tmp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp_path.rename(final_db)
        tmp_manifest.rename(final_manifest)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    print(f"backup: wrote {final_db} ({manifest['bytes']} bytes, sha256={checksum[:16]}…, "
          f"tables={len(report['tables'])}, integrity=ok)")
    if retain > 0:
        for old in sorted(out_dir.glob("*.db"))[: -retain]:
            old.unlink()
            old.with_suffix(".manifest.json").unlink(missing_ok=True)
            print(f"backup: pruned {old.name} (retain={retain})")
    return 0


def cmd_verify(path: Path) -> int:
    if not path.is_file():
        print(f"verify: not found: {path}", file=sys.stderr)
        return 1
    manifest_path = path.with_suffix(".manifest.json")
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"verify: corrupt manifest {manifest_path}: {exc}", file=sys.stderr)
            return 1
        actual = _sha256(path)
        if manifest.get("sha256") != actual:
            print(f"verify: CHECKSUM MISMATCH for {path.name} "
                  f"(manifest={str(manifest.get('sha256'))[:16]}… actual={actual[:16]}…)", file=sys.stderr)
            return 1
        print(f"verify: checksum ok ({actual[:16]}…)")
    else:
        print("verify: no sibling manifest — checksum step skipped (file still integrity-checked)")
    try:
        report = inspect_db(path)
    except sqlite3.Error as exc:
        print(f"verify: cannot open {path}: {exc}", file=sys.stderr)
        return 1
    if not report["integrity_ok"]:
        print(f"verify: integrity_check FAILED: {report['integrity']}", file=sys.stderr)
        return 1
    print(f"verify: {path.name} opens, integrity=ok, "
          f"tables={len(report['tables'])}, rows={sum(v for v in report['row_counts'].values() if v > 0)}")
    return 0


def cmd_restore(backup: Path, db: Path) -> int:
    if cmd_verify(backup) != 0:
        print("restore: refused — the backup did not verify", file=sys.stderr)
        return 1
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.is_file():
        safety = db.with_name(f"{db.name}.pre-restore-{_utc_ts()}.bak")
        shutil.copy2(db, safety)
        print(f"restore: safety copy of the current database → {safety.name}")
        for suffix in ("-wal", "-shm"):
            stale = db.with_name(db.name + suffix)
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    shutil.copy2(backup, db)
    try:
        report = inspect_db(db)
    except sqlite3.Error as exc:
        print(f"restore: restored file does not open: {exc}", file=sys.stderr)
        return 1
    if not report["integrity_ok"]:
        print("restore: restored file failed integrity_check", file=sys.stderr)
        return 1
    print(f"restore: {db} ← {backup.name} (integrity=ok, tables={len(report['tables'])})")
    try:
        db.chmod(0o600)
    except OSError:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup, verify, or restore the Nimna SQLite store.")
    parser.add_argument("--db", default="data/nimna.db", help="live database path (default: data/nimna.db)")
    parser.add_argument("--out-dir", default="data/backups", help="backup directory (default: data/backups)")
    parser.add_argument("--retain", type=int, default=7, help="keep at most N backups (default: 7, 0 = keep all)")
    parser.add_argument("--verify", metavar="FILE", help="verify a backup file (checksum + integrity_check)")
    parser.add_argument("--restore", metavar="FILE", help="restore FILE over --db (keeps a .pre-restore safety copy)")
    args = parser.parse_args(argv)
    if args.verify and args.restore:
        parser.error("--verify and --restore are mutually exclusive")
    if args.verify:
        return cmd_verify(Path(args.verify))
    if args.restore:
        return cmd_restore(Path(args.restore), Path(args.db))
    return cmd_backup(Path(args.db), Path(args.out_dir), args.retain)


if __name__ == "__main__":
    raise SystemExit(main())
