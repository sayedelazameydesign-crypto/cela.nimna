"""Hotspot regression — ترياج S110/S112 (2026-09-25): الموضعان الحساسان خارج الـhygiene.

اكتشاف المراجعة المستقلة (استنساخ + قراءة مباشرة) وتبنّيه بعد تحقق حي:
1. agent.py (كتلة Kill-Switch): عطل الكاشف كان يُتخطى بصمت — الآن audit
   anomaly_check_failed + log.warning (fail-open مرئي، السلوك نفسه).
2. computer.py::_preexec: فشل setsid/setrlimit كان يترك طفلًا بلا سياج
   بصمت — الآن رفض spawn صريح (RuntimeError من preexec → SubprocessError
   في الوالد). عمدًا ليست OSError: except (ValueError, OSError) الخارجي
   كان سيسقط بها في sp.run بلا حدود — المسار الأخطر.
"""
import subprocess
from unittest import mock

import pytest

from nimna.core.state import RunStatus
from nimna.providers.base import ModelResponse, ToolCall


def test_anomaly_detector_crash_is_audited_and_run_continues(agent, provider, monkeypatch):
    class _BrokenDetector:
        def log_call(self, *a, **k):
            raise RuntimeError("detector bug probe")

        def should_kill(self, *a, **k):  # pragma: no cover — لا يصل إليها
            return False, ""

    monkeypatch.setattr("nimna.core.agent.get_detector", lambda: _BrokenDetector())

    with mock.patch(
        "nimna.tools.builtin.computer._run_with_limits",
        return_value=subprocess.CompletedProcess(["echo"], 0, "anomaly-probe\n", ""),
    ):
        provider.queue(
            '{"skills": ["code_execution"], "reason": "r"}',
            ModelResponse(text="", tool_calls=[ToolCall(
                name="shell_execute",
                arguments={"command": "echo anomaly-probe", "purpose": "s110-probe"})]),
            "done")
        result = agent.run("probe", session_id="s110-hotspot")
        resumes = 0
        while getattr(result, "status", None) == RunStatus.AWAITING_APPROVAL and resumes < 3:
            result = agent.resume(result.run_id, True)
            resumes += 1

    events = [e["event"] for e in agent.memory.get_audit("s110-hotspot")]
    assert "anomaly_check_failed" in events          # العطل مرئي في سلسلة الأدلة
    assert "anomaly_kill" not in events              # لم يُنتج قتلًا زائفًا
    assert result.status == RunStatus.DONE           # الجلسة تابعت حياتها (السلوك unchanged)


def test_preexec_rlimit_failure_refuses_spawn_never_falls_back(monkeypatch):
    """فشل setrlimit → رفض الـspawn. الحارس الأهم: يجب ألا يصل التنفيذ إلى
    except (ValueError, OSError) الخارجي (فهو يسقط في sp.run بلا حدود)."""
    import resource

    from nimna.tools.builtin import computer

    def _boom(*a, **k):
        raise OSError(1, "EPERM (container denied setrlimit)")

    monkeypatch.setattr(resource, "setrlimit", _boom)
    # SubprocessError = preexec أجهض الطفل في الوالد. لو سقط الكود في الـfallback
    # لأُعيد CompletedProcess بلا أي استثناء — فيفشل هذا الاختبار فورًا.
    with pytest.raises(subprocess.SubprocessError):
        computer._run_with_limits(["/bin/echo", "hi"], 5)


def test_preexec_normal_path_still_executes():
    """ضابط إيجابي: المسار السليم (حدود تُطبَّق بنجاح) لم يتأثر بالإصلاح."""
    from nimna.tools.builtin import computer

    out = computer._run_with_limits(["/bin/echo", "hi"], 5)
    assert out.returncode == 0 and out.stdout.strip() == "hi"
