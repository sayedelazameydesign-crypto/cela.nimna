"""Every variable Settings reads must be documented in .env.example (offline).

Regression tests: SHELL_TOOL_ENABLED, MODEL_TIMEOUT and friends were read by
nimna/config.py but absent from .env.example, so operators could not discover
them. This pins documentation completeness going forward.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _settings_env_names() -> set[str]:
    text = (REPO / "nimna" / "config.py").read_text(encoding="utf-8")
    names = set(re.findall(r'_env\w*\(\s*"([A-Z][A-Z0-9_]+)"', text))
    # _key_source pairs (primary + fallback aliases)
    for primary, fallback in re.findall(
            r'_key_source\(\s*"([A-Z][A-Z0-9_]+)"\s*,\s*"([A-Z][A-Z0-9_]+)"', text):
        names.add(primary)
        names.add(fallback)
    return names


def _example_names() -> set[str]:
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    # active lines AND commented-out aliases (e.g. "# COMPUTER_VNC_URL=") count
    # as documented — the operator can discover them either way.
    return set(re.findall(r'^#?\s*([A-Z][A-Z0-9_]+)\s*=', text, re.M))


def test_every_settings_variable_is_documented():
    missing = sorted(_settings_env_names() - _example_names())
    assert not missing, (
        "Settings reads these variables but .env.example does not document them: "
        + ", ".join(missing))


def test_documented_shell_flag_matches_code_default():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^SHELL_TOOL_ENABLED=false\s*$", text, re.M), \
        "the shell primitive must stay opt-in in the shipped example"
