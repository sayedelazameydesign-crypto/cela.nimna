"""Offline repository integrity gate for the capability/verification contract."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    ROOT / "docs" / "CAPABILITY-MATRIX.md",
    ROOT / "docs" / "VERIFICATION-MATRIX.md",
    ROOT / "docs" / "browser_use_v4.md",
    ROOT / "nimna" / "models" / "registry.py",
    ROOT / "nimna" / "provenance" / "hashchain.py",
    ROOT / "nimna" / "browser" / "cloud_v4.py",
)


def main() -> int:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_FILES if not path.is_file()]
    if missing:
        print("missing required capability files:", ", ".join(missing), file=sys.stderr)
        return 1
    try:
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", errors="replace").split("\x00")
    except Exception:
        tracked = []
    if any(Path(path).name in {".env", ".env.local", ".env.production"} for path in tracked if path):
        print("refusing to pass: plaintext environment file is tracked", file=sys.stderr)
        return 1
    for path in (ROOT / "nimna", ROOT / "security"):
        for source in path.rglob("*.py"):
            try:
                ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            except SyntaxError as exc:
                print(f"syntax error in {source}: {exc}", file=sys.stderr)
                return 1
    browser_doc = (ROOT / "docs" / "browser_use_v4.md").read_text(encoding="utf-8")
    required_phrases = (
        "X-Browser-Use-API-Key",
        "PATCH /api/v4/browsers/{id}",
        "Retry-After",
        "GPT-6 Astra",
        "BROWSER_USE_MAX_SPEND_USD=0",
    )
    missing_phrases = [phrase for phrase in required_phrases if phrase not in browser_doc]
    if missing_phrases:
        print("browser V4 doc missing: " + ", ".join(missing_phrases), file=sys.stderr)
        return 1
    print(f"integrity PASS: {len(REQUIRED_FILES)} capability files and Python syntax verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
