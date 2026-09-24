"""T7.1-C — SHELL_TOOL_ENABLED: افتراضي معطَّل، وshell غير قابل للوصول
без gateway ولا مهارة (gateway=None + legacy + بلا مهارة ⇒ غير قابل للوصول)."""
from __future__ import annotations

from unittest import mock
import pytest
from nimna.core.state import RunState
from nimna.providers.base import ToolCall

from nimna.config import Settings
from nimna.memory import MemoryStore
from nimna.providers import MockProvider
from nimna.skills import SkillManager
from nimna.core.agent import Agent
from nimna.core.approval import DeferToClient
from nimna.tools import default_registry
from nimna.tools.base import ToolContext


def _bare_agent(settings, tmp_path):
    """وكيل عاري: بلا gateway (None)، مسار legacy، ومهارات من مجلد فارغ."""
    empty_skills = tmp_path / "no-skills"
    empty_skills.mkdir(exist_ok=True)
    skills = SkillManager(empty_skills)          # لا مهارة محمَّلة إطلاقاً
    assert len(skills._skills) == 0
    settings.shell_tool_enabled = False          # الافتراضي الصريح
    return Agent(MockProvider(), skills, default_registry(), MemoryStore(":memory:"),
                 settings, approval_policy=DeferToClient(),
                 workspace=settings.workspace_dir)


def test_default_is_off_all_three_layers(settings):
    """الأمر الحي: الافتراضي False في الطبقات الثلاث."""
    fresh = Settings.from_env(env_file=None)
    assert fresh.shell_tool_enabled is False                      # config/runtime
    import dataclasses, nimna.config as cfg, inspect
    (f,) = [f for f in dataclasses.fields(cfg.Settings) if f.name == "shell_tool_enabled"]
    assert f.default is False                                     # تعريف الحقل
    src = inspect.getsource(cfg.Settings.from_env)
    assert '_env_bool("SHELL_TOOL_ENABLED", False)' in src        # env default


def test_no_gateway_no_skill_shell_is_unreachable(settings, tmp_path, monkeypatch):
    """التركيبة الممنوعة: gateway=None + legacy + بلا مهارة ⇒ shell المحلي
    (run_command — حزمة shell المقيدة بالـ workspace) غير قابل للوصول.
    ملاحظة النطاق: اسم shell_execute في الـ registry يعود لحزمة computer
    (bash داخل حاوية سطح المكتب المعزولة) — خارج نطاق هذا البند ولا يُدّعى عنه شيء هنا."""
    monkeypatch.delenv("SHELL_TOOL_ENABLED", raising=False)   # الافتراضي
    agent = _bare_agent(settings, tmp_path)
    # 1) غير مُسجَّل في الـ registry أصلاً (بوابة التسجيل تقرأ env)
    names = [d["name"] for d in agent.tools.describe()]
    assert "run_command" not in names
    # 2) حتى لو حاول أحدهم التنفيذ المباشر عبر الـ registry ⇒ مرفوض بلا fallback
    #    (execute لا يرفع استثناءً — يُرجع خطأ مُسلسلاً ok=False)
    res, ok, _ = agent.tools.execute(
        "run_command", {"command": "echo hi"},
        ToolContext(settings=settings, workspace=settings.workspace_dir,
                    session_id="c-neg"))
    assert ok is False and "unknown tool" in res
    # 3) ومسار المهارات لا يعرضه: allowed_tools فارغ بلا مهارات
    from nimna.core.state import RunState
    st = RunState(run_id="c-neg", user_id="t", session_id="c-neg", user_message="x")
    agent._refresh_allowed_tools(st)
    assert "run_command" not in st.allowed_tools
    assert all("shell" not in t for t in st.allowed_tools)


def test_registration_gate_reads_env_while_handler_gate_reads_settings(settings, tmp_path, monkeypatch):
    """البوابتان حقيقيتان ومنفصلتان: التسجيل يقرأ env مباشرة، والمعالج يقرأ
    Settings — الاثنان افتراضياً معطّلان معاً (يُثبت اختباراً لا وصفاً)."""
    from nimna.tools.builtin import shell as shell_mod
    from nimna.tools import ToolRegistry
    monkeypatch.setenv("SHELL_TOOL_ENABLED", "1")
    reg = default_registry()
    assert "run_command" in [d["name"] for d in reg.describe()]     # env فتحت التسجيل
    monkeypatch.delenv("SHELL_TOOL_ENABLED", raising=False)
    empty = tmp_path / "ns2"; empty.mkdir(exist_ok=True)
    settings.shell_tool_enabled = False
    agent = Agent(MockProvider(), SkillManager(empty), reg, MemoryStore(":memory:"),
                  settings, approval_policy=DeferToClient(), workspace=settings.workspace_dir)
    # والمعالج برضه يرفض ما دامت Settings معطلة (البوابة الثانية): النتيجة DENIED لا تنفيذ
    from nimna.tools.builtin.shell import ShellRequest
    res = shell_mod.execute_shell(ShellRequest(command="echo hi"), settings=settings,
                                  workspace_root=settings.workspace_root
                                  if hasattr(settings, "workspace_root") else settings.workspace_dir,
                                  session_id="c2")
    assert res.status == shell_mod.ShellStatus.DENIED


def test_legacy_handler_denies_even_when_reached(settings):
    """الحد الثاني: المعالج legacy نفسه يرفض ما دام الـ flag معطلاً (لا fallback)."""
    from nimna.tools.builtin import shell as shell_mod
    from nimna.tools.builtin.shell import ShellRequest
    res = shell_mod.execute_shell(ShellRequest(command="echo hi"),
                                  settings=settings,
                                  workspace_root=settings.workspace_dir,
                                  session_id="c-legacy")
    assert res.status == shell_mod.ShellStatus.DENIED
    assert "SHELL_TOOL_ENABLED" in res.reason
