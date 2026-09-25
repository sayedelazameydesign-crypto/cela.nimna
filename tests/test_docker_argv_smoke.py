"""T7.1-E — Docker argv smoke (host-side contract of run_in_docker).

ترويسة إلزامية: هذا الاختبار **لا يغطي ما يحدث داخل الحاوية** — هو دخان
argv من جهة المضيف فقط (subprocess مُحوَّل). الحد الأمني الفعلي داخل
الحاوية (kernel namespaces/cgroups) **بلا سقف انحدار آلي** هنا: من يعدّل
argv يُمسكه الاختبار، ومن يعدّل صورة/daemon/dockerd لا يُمسكه شيء آلي.
"""
from __future__ import annotations

from unittest import mock

import pytest

from nimna.tools import sandbox


# ---------------------------------------------------------------- pins ---
# الأربعة المثبّتة + مكان فرض كل عنصر (argv-index أو subprocess-kwarg)
def _smoke_pins(cmd: list[str], kwargs: dict) -> None:
    """يرفض أي انحدار في الأربعة + يثبت أين يُفرض timeout."""
    def at(*needle: str) -> int:
        """index of needle sequence inside cmd — proof it lives IN ARGV."""
        for i in range(len(cmd) - len(needle) + 1):
            if cmd[i:i + len(needle)] == list(needle):
                return i
        raise AssertionError(f"missing from argv: {needle} (argv={cmd})")

    # 1) شبكة مقطوعة — قيمة داخل argv
    i = at("--network"); assert cmd[i + 1] == "none", (i, cmd[i + 1])
    # 2) سقوف موارد — قيم داخل argv
    i = at("--memory"); assert cmd[i + 1].endswith("m"), cmd[i + 1]
    i = at("--cpus");   assert cmd[i + 1] == "1"
    i = at("--pids-limit"); assert cmd[i + 1].isdigit()
    # 3) امتيازات مقطوعة — قيم داخل argv
    i = at("--cap-drop"); assert cmd[i + 1] == "ALL"
    i = at("--security-opt"); assert cmd[i + 1] == "no-new-privileges"
    # 4) جذر نظام ملفات محصور — داخل argv
    i = at("-v"); assert cmd[i + 1].endswith(":/work"), cmd[i + 1]
    i = at("-w"); assert cmd[i + 1] == "/work"
    # timeout — **ليس في argv**: يُفرض kwarg للـ subprocess نفسه، والاختبار
    # يقرأ الـ kwarg (سقف = sandbox timeout + 15s هامش قتل خارجي)
    assert "timeout" in kwargs and kwargs["timeout"] > 0, \
        "timeout must be a subprocess.run kwarg"


# -------------------------------------------------------- production path ---
def test_production_docker_argv_carries_all_pins_and_timeout_is_a_kwarg():
    """المسار الحقيقي: run_python_code بـ backend=docker → run_in_docker → subprocess.run."""
    captured: dict = {}

    def fake_run(cmd, **kwargs):  # يتقاطع مكان subprocess.run في الإنتاج
        captured["cmd"], captured["kwargs"] = list(cmd), kwargs
        return mock.Mock(returncode=0, stdout="ok", stderr="")

    with mock.patch.object(sandbox, "docker_available", return_value=True), \
         mock.patch.object(sandbox.subprocess, "run", side_effect=fake_run):
        result = sandbox.run_python_code(
            "print(1)",
            type("S", (), {"sandbox_backend": "docker", "sandbox_memory_mb": 256,
                           "sandbox_image": "python:3.11-slim", "sandbox_timeout": 30})(),
            sandbox.Path("/tmp/whatever-ws"),
        )
    assert result.backend == "docker"
    _smoke_pins(captured["cmd"], captured["kwargs"])
    # إثبات صريح لمكان كل عنصر:
    assert "--network" in captured["cmd"] and "timeout" not in captured["cmd"]
    assert captured["kwargs"]["timeout"] == 45  # 30 + 15 grace


# ------------------------------------------------------------- negatives ---
# إزالة كل عنصر ⇒ الدخان يفشل (اختبار سلبي ×4)
def _strip(pin: list[str]) -> list[str]:
    base = ["docker", "run", "--rm", "-i", "--name", "nimna-sbx-x",
            "--network", "none", "--memory", "256m", "--cpus", "1",
            "--pids-limit", "128", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", "/ws:/work", "-w", "/work", "python:3.11-slim", "python", "-"]
    for k in range(len(base) - len(pin) + 1):
        if base[k:k + len(pin)] == pin:
            return base[:k] + base[k + len(pin):]
    raise AssertionError


def test_negative_net_unpinned_fails():
    with pytest.raises(AssertionError, match="missing from argv"):
        _smoke_pins(_strip(["--network", "none"]), {"timeout": 45})


def test_negative_caps_unpinned_fails():
    with pytest.raises(AssertionError):
        _smoke_pins(_strip(["--cap-drop", "ALL"]), {"timeout": 45})


def test_negative_memory_unpinned_fails():
    with pytest.raises(AssertionError):
        _smoke_pins(_strip(["--memory", "256m"]), {"timeout": 45})


def test_negative_workdir_unpinned_fails():
    with pytest.raises(AssertionError):
        _smoke_pins(_strip(["-w", "/work"]), {"timeout": 45})


def test_negative_timeout_kwarg_removed_fails():
    """لو حذف أحد timeout kwarg ⇒ الدخان يفشل (الفرض في الـ kwarg يُقرأ)."""
    cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--memory",
           "256m", "--cpus", "1", "--pids-limit", "128", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "-v", "/ws:/work", "-w",
           "/work", "python:3.11-slim", "python", "-"]
    with pytest.raises(AssertionError, match="kwarg"):
        _smoke_pins(cmd, {"capture_output": True})  # timeout مفقود
