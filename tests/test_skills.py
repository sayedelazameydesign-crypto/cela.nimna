from pathlib import Path

import pytest

from nimna.skills import SkillManager, SkillParseError, parse_skill_text, split_front_matter


def test_front_matter_parsing():
    text = """---
name: demo_skill
description: Does demo things.
triggers: [demo, "test"]
allowed-tools: read_file write_file
version: 2
---

## Instructions
Do it.
"""
    skill = parse_skill_text(text, path=Path("x/SKILL.md"), directory=Path("x"))
    assert skill.name == "demo_skill"
    assert skill.meta.triggers == ["demo", "test"]
    assert skill.meta.allowed_tools == ["read_file", "write_file"]  # agentskills-style alias
    assert skill.meta.version == "2"
    assert skill.instructions.startswith("## Instructions")


def test_lenient_front_matter_with_unquoted_colon():
    text = "---\nname: colon_skill\ndescription: Analyse: everything, always\ntriggers:\n  - a\n  - b\n---\nbody"
    skill = parse_skill_text(text, path=Path("c/SKILL.md"), directory=Path("c"))
    assert skill.meta.description == "Analyse: everything, always"
    assert skill.meta.triggers == ["a", "b"]


def test_name_defaults_to_folder_and_invalid_name_rejected():
    skill = parse_skill_text("---\ndescription: x\n---\nbody", path=Path("my_skill/SKILL.md"), directory=Path("my_skill"))
    assert skill.name == "my_skill"
    with pytest.raises(SkillParseError):
        parse_skill_text("---\nname: Bad Name!\ndescription: x\n---\n", path=Path("b/SKILL.md"), directory=Path("b"))


def test_no_front_matter_uses_first_paragraph():
    data, body = split_front_matter("# Title\n\nFirst paragraph here.\n")
    assert data == {}
    skill = parse_skill_text(body, path=Path("p/SKILL.md"), directory=Path("p"))
    assert skill.meta.description == "First paragraph here."


def test_bundled_skills_load_without_errors(skills: SkillManager):
    assert not skills.errors
    assert {"csv_analysis", "report_writer", "web_research", "python_executor", "file_analysis", "skill_author"} <= set(skills.names())
    assert "references/statistics_guide.md" in skills.get("csv_analysis").meta.references
    assert "csv_analysis" in skills.catalog_text()


def test_keyword_ranking_arabic_and_english(skills: SkillManager):
    ranked = [name for name, _ in ((m.name, s) for m, s in skills.rank_by_keywords("حلّل ملف المبيعات وأنشئ لي تقريرًا"))]
    assert "csv_analysis" in ranked[:2] and "report_writer" in ranked[:2]
    ranked_en = [m.name for m, _ in skills.rank_by_keywords("search the web for the latest news")]
    assert ranked_en[0] == "web_research"


def test_reference_reading_is_jailed(skills: SkillManager):
    text = skills.read_reference("csv_analysis", "references/statistics_guide.md")
    assert "mean" in text
    with pytest.raises(ValueError):
        skills.read_reference("csv_analysis", "../../pyproject.toml")
    with pytest.raises(FileNotFoundError):
        skills.read_reference("csv_analysis", "references/nope.md")


def test_invalid_skill_is_reported_not_fatal(tmp_path: Path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\ndescription: fine\n---\nok", encoding="utf-8")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: BAD NAME\ndescription: x\n---\n", encoding="utf-8")
    manager = SkillManager(tmp_path)
    assert manager.names() == ["good"]
    assert "bad" in manager.errors
