"""Gate for the GitHub ↔ FastAPI Cloud contract: the real file must pass, and
the classic linking mistakes must *break* it (a check that cannot fail proves
nothing). Mutations run against a temp copy — the shipped files are never
rewritten.
"""
from __future__ import annotations

import importlib.util
import io
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_fastapi_cloud_link.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_fastapi_cloud_link", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_contract(tmp_path: Path, text: str) -> tuple[int, str]:
    module = _load()
    contract = tmp_path / "fastapi-cloud.yaml"
    contract.write_text(text, encoding="utf-8")
    module.CONTRACT = contract
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        code = module.main()
    return code, buf.getvalue()


def test_shipped_contract_passes() -> None:
    module = _load()
    assert module.CONTRACT == ROOT / "fastapi-cloud.yaml"
    assert module.WORKFLOW == ROOT / ".github" / "workflows" / "fastapi-cloud-deploy.yml"
    assert module.main() == 0


def test_missing_contract_fails(tmp_path: Path) -> None:
    module = _load()
    module.CONTRACT = tmp_path / "absent.yaml"
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        code = module.main()
    assert code == 1
    assert "مفقود" in buf.getvalue()


def test_secret_literal_in_contract_is_rejected(tmp_path: Path) -> None:
    secret = "AIzaSyDUMMYDUMMYDUMMYDUMMY12345"
    shipped = (ROOT / "fastapi-cloud.yaml").read_text(encoding="utf-8")
    poisoned = shipped.replace(
        "  - key: GEMINI_API_KEY\n    secret: true",
        f"  - key: GEMINI_API_KEY\n    secret: true\n    # {secret}",
    )
    code, log = _run_contract(tmp_path, poisoned)
    assert code == 1
    assert "سرّي" in log
    assert secret not in log


def test_env_typo_is_rejected(tmp_path: Path) -> None:
    shipped = (ROOT / "fastapi-cloud.yaml").read_text(encoding="utf-8")
    code, log = _run_contract(tmp_path, shipped.replace("MODEL_PROVIDER", "MODEL_PROVIDERR"))
    assert code == 1
    assert "لا يقرئه الكود" in log


def test_production_guards_cannot_be_relaxed(tmp_path: Path) -> None:
    shipped = (ROOT / "fastapi-cloud.yaml").read_text(encoding="utf-8")
    mutated = (
        shipped.replace('value: "production"', 'value: "development"', 1)
        .replace('value: "0"', 'value: "99"', 1)
        .replace('value: "false"', 'value: "true"', 1)
    )
    code, log = _run_contract(tmp_path, mutated)
    assert code == 1
    assert "NIMNA_ENV" in log
    assert "MAX_SPEND_USD" in log
    assert "AGENT_AUTO_APPROVE" in log


def test_docker_paths_are_rejected(tmp_path: Path) -> None:
    shipped = (ROOT / "fastapi-cloud.yaml").read_text(encoding="utf-8")
    code, log = _run_contract(tmp_path, shipped.replace('value: "skills"', 'value: "/app/skills"', 1))
    assert code == 1
    assert "Docker" in log


def test_secret_must_not_have_a_value(tmp_path: Path) -> None:
    shipped = (ROOT / "fastapi-cloud.yaml").read_text(encoding="utf-8")
    mutated = shipped.replace(
        "  - key: NIMNA_API_KEY\n    secret: true",
        '  - key: NIMNA_API_KEY\n    secret: true\n    value: "not-a-real-key-but-still-forbidden"',
    )
    code, log = _run_contract(tmp_path, mutated)
    assert code == 1
    assert "NIMNA_API_KEY" in log


def test_ignore_must_not_drop_skills(tmp_path: Path) -> None:
    module = _load()
    ignore = tmp_path / ".fastapicloudignore"
    ignore.write_text((ROOT / ".fastapicloudignore").read_text(encoding="utf-8") + "\nskills/\n", encoding="utf-8")
    module.IGNORE = ignore
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        code = module.main()
    assert code == 1
    assert "skills/" in buf.getvalue()


def test_wrong_health_path_is_rejected(tmp_path: Path) -> None:
    shipped = (ROOT / "fastapi-cloud.yaml").read_text(encoding="utf-8")
    code, log = _run_contract(tmp_path, shipped.replace('health_path: "/api/health"', 'health_path: "/api/healthz"'))
    assert code == 1
    assert "health_path" in log


def test_broken_yaml_is_rejected(tmp_path: Path) -> None:
    code, log = _run_contract(tmp_path, "link: [\n")
    assert code == 1
    assert "YAML" in log or "غير صالح" in log


def test_workflow_must_name_official_secrets() -> None:
    workflow = ROOT / ".github" / "workflows" / "fastapi-cloud-deploy.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "FASTAPI_CLOUD_TOKEN" in text
    assert "FASTAPI_CLOUD_APP_ID" in text
    assert "uv run fastapi deploy" in text
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "workflow_dispatch" in text
    assert "fastapi-cloud[bot]" in text
    assert re.search(r"(?ms)^on:.*?^\s+push:", text) is None
