"""T8.1 — Production gate hardening, pinned by falsifiable offline tests.

The gate itself must not be mutable back into a rubber stamp: these tests
assert the CI workflow carries NO masks, the T8 ancestor requirement, the
docker/health steps, and — adversarially — that the falsifiability checker
REJECTS a tampered workflow.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github/workflows/ci.yml"
T8_SHA = "9885f3c33b908b131855db4e8ac1277c881ea56c"


def _ci_text() -> str:
    return CI.read_text(encoding="utf-8")


def test_ci_workflow_parses_and_carries_the_gate_job():
    doc = yaml.safe_load(_ci_text())
    assert "production-gate" in doc["jobs"]
    gate = doc["jobs"]["production-gate"]
    assert gate["needs"] == ["test", "security-blocklist", "trivy", "zap", "sandbox-escape"]


def test_ci_has_no_masks_left():
    text = _ci_text()
    assert "continue-on-error" not in text, "advisory masking re-entered CI"
    assert "|| true" not in text, "'|| true' re-entered CI — an invariant that cannot fail"
    assert "fail_action: false" not in text, "scanner failures must not be silenced"


def test_gate_pins_the_t8_ancestor_and_docker_and_health():
    text = _ci_text()
    assert "production_gate.sh" in text, "CI must run THE gate script"
    assert "fetch-depth: 0" in text, "ancestor check needs real history"
    gate_script = (REPO / "scripts/production_gate.sh").read_text(encoding="utf-8")
    assert T8_SHA in gate_script, "the T8 pin lives in ONE place: the gate script"
    assert "merge-base --is-ancestor" in gate_script
    assert "docker build --pull" in gate_script
    # health must HARD-FAIL (the soft pattern '... | head -c' alone is banned)
    assert "curl -sf" in gate_script and "server never answered /api/health" in gate_script
    assert "--skip" not in gate_script, "the gate has no escape hatch by design"


def test_dockerfile_toolchain_is_hardened():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert '"pip>=26.2"' in dockerfile and '"setuptools>=83"' in dockerfile, \
        "shipped image must not carry the vulnerable pip/setuptools (PYSEC-2026-*)"


def test_falsifiability_checker_passes_on_real_ci():
    rc = subprocess.run(["bash", str(REPO / "scripts/check_ci_falsifiable.sh"), str(CI)],
                        capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr


def test_falsifiability_checker_rejects_tampered_workflow(tmp_path):
    # ADVERSARIAL: re-insert each mask into a copy — the checker must go RED
    original = _ci_text()
    for mask in ("continue-on-error: true", "run: foo || true", "fail_action: false"):
        bad = tmp_path / "bad.yml"
        bad.write_text(original + f"\n  stray: |\n    {mask}\n", encoding="utf-8")
        rc = subprocess.run(["bash", str(REPO / "scripts/check_ci_falsifiable.sh"), str(bad)],
                            capture_output=True, text=True)
        assert rc.returncode != 0, f"checker accepted the mask: {mask}"
        assert "VIOLATION" in rc.stdout


def test_t8_is_actually_an_ancestor_of_the_current_head():
    rc = subprocess.run(["git", "merge-base", "--is-ancestor", T8_SHA, "HEAD"],
                        cwd=REPO, capture_output=True)
    assert rc.returncode == 0, "this repo's HEAD must contain the T8 commit"


def test_ancestor_gate_goes_red_on_a_fake_sha():
    rc = subprocess.run(["git", "merge-base", "--is-ancestor",
                         "0" * 40, "HEAD"], cwd=REPO, capture_output=True)
    assert rc.returncode != 0, "a fake commit must NOT satisfy the ancestor gate"


def test_health_endpoint_is_real_runtime_state():
    """The endpoint the gate curls returns REAL runtime evidence (offline import)."""
    from fastapi.testclient import TestClient
    import os
    os.environ.setdefault("MODEL_PROVIDER", "mock")
    os.environ.setdefault("DB_PATH", ":memory:")
    from nimna.api.app import create_app  # noqa: import path must stay stable
    client = TestClient(create_app())
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["provider"] == "mock"
    assert isinstance(body["tools"], int) and body["tools"] >= 28  # T8 tools present
