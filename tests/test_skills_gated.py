"""Capability-gated tools are reported as gated, not unknown (offline).

Regression tests: `nimna skills validate` and `nimna doctor` used to warn that
the shell_execution skill "references unknown tools: run_command" whenever the
SHELL_TOOL_ENABLED flag was off — even though that is the designed opt-in
state. Gated tools now get their own accurate message.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_gated_map_matches_shell_module_constants():
    from nimna.tools.builtin import shell

    from nimna.skills.manager import SkillManager

    import inspect

    source = inspect.getsource(SkillManager.validate_all)
    assert shell.TOOL_NAME in source and shell.ENV_FLAG in source


def test_shell_skill_reports_gated_not_unknown_when_flag_off(monkeypatch):
    from nimna.skills.manager import SkillManager
    from nimna.tools import default_registry

    monkeypatch.delenv("SHELL_TOOL_ENABLED", raising=False)
    manager = SkillManager(REPO / "skills")
    report = manager.validate_all(registry_names=set(default_registry().names()))
    shell_warnings = report.get("shell_execution", [])
    assert not any("unknown tools" in w for w in shell_warnings), shell_warnings
    assert any("SHELL_TOOL_ENABLED" in w and "gated" in w for w in shell_warnings), shell_warnings


def test_shell_skill_is_clean_when_flag_on(monkeypatch):
    from nimna.skills.manager import SkillManager
    from nimna.tools import default_registry

    monkeypatch.setenv("SHELL_TOOL_ENABLED", "true")
    manager = SkillManager(REPO / "skills")
    report = manager.validate_all(registry_names=set(default_registry().names()))
    assert report.get("shell_execution", []) == []


def test_truly_unknown_tools_still_flagged(tmp_path):
    from nimna.skills.manager import SkillManager

    skill_dir = tmp_path / "skills" / "broken_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: broken_skill\ndescription: broken\nversion: 1.0.0\n"
        "allowed_tools: [no_such_tool_xyz]\n---\n\nDo things.\n",
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path / "skills")
    report = manager.validate_all(registry_names={"list_files"})
    assert any("unknown tools" in w and "no_such_tool_xyz" in w
               for w in report["broken_skill"]), report
