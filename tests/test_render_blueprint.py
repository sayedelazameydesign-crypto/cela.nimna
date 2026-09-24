"""Gate for the Render blueprint: the real file must pass, and the classic deploy
mistakes must *break* it (a check that cannot fail proves nothing).

يُحمَّل `scripts/check_render_blueprint.py` كوحدة، وتُستبدل نقطة قراءة render.yaml
بملف مؤقت — لذلك الفحوص السلبية لا تلمس المستودع.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_render_blueprint.py"

BASE = """
services:
  - type: web
    name: cela-nimna
    runtime: docker
    plan: free
    dockerfilePath: ./Dockerfile
    dockerContext: .
    healthCheckPath: /api/health
    autoDeployTrigger: "off"
    envVars:
      - key: MODEL_PROVIDER
        value: "gemini"
      - key: MAX_SPEND_USD
        value: "0"
      - key: GEMINI_API_KEY
        sync: false
"""


def _load():
    spec = importlib.util.spec_from_file_location("check_render_blueprint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path, text: str) -> tuple[int, str]:
    module = _load()
    blueprint = tmp_path / "render.yaml"
    blueprint.write_text(text, encoding="utf-8")
    module.BLUEPRINT = blueprint
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main()
    return code, err.getvalue() + out.getvalue()


def test_shipped_blueprint_passes() -> None:
    module = _load()
    assert module.BLUEPRINT == ROOT / "render.yaml", "expected the checker to target the repo blueprint"
    assert module.main() == 0


def test_missing_blueprint_is_a_skip(tmp_path) -> None:
    module = _load()
    module.BLUEPRINT = tmp_path / "absent.yaml"
    assert module.main() == 0  # branch without a deploy contract must not be blocked by it


def test_yaml_boolean_trigger_is_rejected(tmp_path) -> None:
    code, log = _run(tmp_path, BASE.replace('autoDeployTrigger: "off"', "autoDeployTrigger: off"))
    assert code == 1 and "autoDeployTrigger" in log


def test_deprecated_autodeploy_key_is_rejected(tmp_path) -> None:
    code, log = _run(tmp_path, BASE.replace('autoDeployTrigger: "off"', "autoDeploy: false"))
    assert code == 1 and "مهجور" in log


def test_unquoted_scalar_value_is_rejected(tmp_path) -> None:
    # أرقام/booleans بلا اقتباس: YAML 1.1 يعيد scalar غير نصي وRender يتوقع نصاً.
    mutated = BASE.replace('value: "0"', "value: 0").replace(
        "      - key: GEMINI_API_KEY",
        "      - key: AGENT_VERIFY\n        value: true\n      - key: GEMINI_API_KEY",
    )
    code, log = _run(tmp_path, mutated)
    assert code == 1
    assert "MAX_SPEND_USD" in log and "AGENT_VERIFY" in log
    assert "gemini" not in log  # value: gemini هو نص صالح، لا يُعاقَب عليه


def test_inlined_secret_is_rejected(tmp_path) -> None:
    secret = "AIzaSyDUMMYDUMMYDUMMYDUMMY12345"
    code, log = _run(tmp_path, BASE.replace("      - key: GEMINI_API_KEY\n        sync: false",
                                           f"      - key: GEMINI_API_KEY\n        value: {secret}"))
    assert code == 1
    assert "sync: false" in log          # the rule it broke
    assert secret not in log            # the checker never echoes the secret back


def test_env_name_the_code_never_reads_is_rejected(tmp_path) -> None:
    code, log = _run(tmp_path, BASE.replace("MODEL_PROVIDER", "MODEL_PROVIDERR"))
    assert code == 1 and "لا يقرئه الكود" in log


def test_health_path_must_be_a_real_route(tmp_path) -> None:
    code, log = _run(tmp_path, BASE.replace("/api/health", "/api/healthz"))
    assert code == 1 and "healthCheckPath" in log


def test_free_plan_rejects_persistent_disk(tmp_path) -> None:
    mutated = BASE.replace("    healthCheckPath:", "    disk:\n      name: data\n      mountPath: /data\n      sizeGB: 10\n    healthCheckPath:")
    code, log = _run(tmp_path, mutated)
    assert code == 1 and "قرص دائم" in log


def test_broken_yaml_or_empty_services_is_rejected(tmp_path) -> None:
    assert _run(tmp_path, "services: []\n")[0] == 1
    assert _run(tmp_path, "services:\n  - type: web\n   name: bad-indent\n")[0] == 1


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("plan: free", "plan="),                    # خطة غير موجودة في Render
        ("runtime: docker", "runtime="),            # runtime غير مدعوم
        ("type: web", "type="),                     # نوع خدمة غير معروف
    ],
)
def test_unknown_enum_values_are_rejected(tmp_path, mutation: str, expected: str) -> None:
    bad = {"plan: free": "plan: hacker", "runtime: docker": "runtime: docker-compose",
           "type: web": "type: gui"}[mutation]
    code, log = _run(tmp_path, BASE.replace(mutation, bad, 1))
    assert code == 1 and expected in log
