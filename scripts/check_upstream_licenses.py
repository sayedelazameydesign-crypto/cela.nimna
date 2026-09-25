#!/usr/bin/env python3
"""Upstream license-header guard (stdlib-only).

Harness ships new PolyForm headers by *porting* old proprietary UI files
(a 2020 header landed in 2024, a 2021 header in 2025 — see
docs/FREE-STACK.md), so pin-to-tag alone is not enough: re-run this check
on every upgrade and diff the file list. Substring matching (not header-only) is deliberate: any new mention deserves human review, and enumerating license names could miss a future variant; benign hits bump the baseline with a documented reason.

Usage:
    python scripts/check_upstream_licenses.py --tree /path/to/harness --baseline 27
    python scripts/check_upstream_licenses.py --clone https://github.com/harness/harness --baseline 27
    python scripts/check_upstream_licenses.py --tree /path/to/forgejo --baseline 0 --exclude options/license

Exit 0 when the marker count is at or below the baseline, exit 1 above it
(the new/extra files are printed for legal review).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_MARKER = "PolyForm"


def count_markers(tree: Path, marker: str = DEFAULT_MARKER,
                  exclude: tuple[str, ...] = ()) -> list[str]:
    """Return sorted repo-relative paths of files containing ``marker``.

    Case-insensitive, skips ``.git`` and any ``exclude`` dir prefixes.
    Binary files are read with errors ignored — a header is ASCII anyway.
    """
    hits: list[str] = []
    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(tree).as_posix()
        if rel.startswith(".git/") or rel == ".git":
            continue
        if any(rel == ex or rel.startswith(ex.rstrip("/") + "/") for ex in exclude):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if marker.lower() in text.lower():
            hits.append(rel)
    return hits


def check(tree: Path, baseline: int, marker: str = DEFAULT_MARKER,
          exclude: tuple[str, ...] = ()) -> tuple[int, list[str]]:
    """Return ``(exit_code, hits)``; 0 when ``len(hits) <= baseline``."""
    hits = count_markers(tree, marker, exclude)
    return (0 if len(hits) <= baseline else 1, hits)


def shallow_clone(url: str, ref: str | None = None) -> Path:
    """Shallow-clone ``url`` into a temp dir and return its path."""
    dest = Path(tempfile.mkdtemp(prefix="upstream-license-"))
    cmd = ["git", "clone", "--quiet", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    subprocess.run(cmd + [url, str(dest)], check=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--tree", type=Path, help="local checkout to scan")
    src.add_argument("--clone", help="git URL to shallow-clone and scan")
    parser.add_argument("--ref", default=None, help="tag/branch for --clone")
    parser.add_argument("--baseline", type=int, required=True,
                        help="max tolerated marker files (fail above)")
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument("--exclude", action="append", default=[],
                        help="dir prefix to skip (repeatable)")
    args = parser.parse_args(argv)

    tree = args.tree if args.tree else shallow_clone(args.clone, args.ref)
    code, hits = check(tree, args.baseline, args.marker, tuple(args.exclude))
    print(f"marker={args.marker} baseline={args.baseline} found={len(hits)}")
    for rel in hits:
        print(f"  {rel}")
    if code:
        print(f"FAIL: {len(hits)} > baseline {args.baseline} — review before merging the upgrade",
              file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
