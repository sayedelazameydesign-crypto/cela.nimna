"""Upstream license guard contract (offline — fixture trees, no clones).

``scripts/check_upstream_licenses.py`` turns the Gitness finding
("recurring port-driven PolyForm contamination") into a permanent guard:
count marker headers, fail above the pinned baseline. These tests pin the
counting, baseline, exclusion, and CLI-exit behavior against synthetic
trees; the monthly ``upstream-license-watch`` workflow runs the same
script against the live upstreams.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_upstream_licenses.py"

sys.path.insert(0, str(REPO / "scripts"))
from check_upstream_licenses import check, count_markers  # noqa: E402


APACHE_HDR = "Licensed under the Apache License, Version 2.0"
POLYFORM_HDR = "Use of this source code is governed by the PolyForm Shield 1.0.0 license"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "upstream"
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def test_clean_tree_counts_zero(tmp_path):
    root = _tree(tmp_path, {"a.go": APACHE_HDR + "\npackage x\n", "b.ts": "// mit\n"})
    assert count_markers(root) == []


def test_counts_marker_case_insensitively(tmp_path):
    root = _tree(tmp_path, {
        "a.ts": POLYFORM_HDR + "\n",
        "b.ts": POLYFORM_HDR.lower() + "\n",
        "c.ts": POLYFORM_HDR.upper() + "\n",
    })
    assert count_markers(root) == ["a.ts", "b.ts", "c.ts"]


def test_git_dir_is_skipped(tmp_path):
    root = _tree(tmp_path, {".git/objects/pack/p": POLYFORM_HDR + "\n"})
    assert count_markers(root) == []


def test_exclude_prefix_skips_license_templates(tmp_path):
    root = _tree(tmp_path, {
        "options/license/PolyForm-Shield-1.0.0": POLYFORM_HDR + "\n",
        "web/src/app.ts": POLYFORM_HDR + "\n",
    })
    assert count_markers(root, exclude=("options/license",)) == ["web/src/app.ts"]


def test_binary_files_do_not_crash_and_still_match(tmp_path):
    root = tmp_path / "upstream"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"\x00\x01" + POLYFORM_HDR.encode() + b"\xff\xfe")
    assert count_markers(root) == ["blob.bin"]


@pytest.mark.parametrize(("n", "baseline", "code"), [(0, 0, 0), (26, 26, 0), (27, 26, 1), (1, 0, 1)])
def test_baseline_boundary(tmp_path, n, baseline, code):
    files = {f"f{i}.ts": POLYFORM_HDR + "\n" for i in range(n)}
    got, hits = check(_tree(tmp_path, files), baseline)
    assert got == code
    assert len(hits) == n


def test_cli_exit_codes_and_report(tmp_path):
    root = _tree(tmp_path, {"x.ts": POLYFORM_HDR + "\n"})
    ok = subprocess.run([sys.executable, str(SCRIPT), "--tree", str(root), "--baseline", "1"],
                        capture_output=True, text=True)
    assert ok.returncode == 0
    assert "found=1" in ok.stdout
    bad = subprocess.run([sys.executable, str(SCRIPT), "--tree", str(root), "--baseline", "0"],
                         capture_output=True, text=True)
    assert bad.returncode == 1
    assert "x.ts" in bad.stdout  # offending files are listed for review
    assert "FAIL" in bad.stderr


def test_watch_workflow_pins_baselines():
    import yaml

    wf = yaml.safe_load(
        (REPO / ".github" / "workflows" / "upstream_license_watch.yml").read_text(encoding="utf-8"))
    runs = [s["run"] for j in wf["jobs"].values() for s in j["steps"] if "run" in s]
    assert any("--baseline 27" in r and "harness/harness" in r for r in runs)
    assert any("--baseline 0" in r and "mirror-forgejo" in r for r in runs)
    assert SCRIPT.exists()  # the workflow's script must exist
