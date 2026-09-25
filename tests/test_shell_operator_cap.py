"""SHELL_TIMEOUT_MS is an operator ceiling, not decoration (offline).

Regression tests: Settings parsed SHELL_TIMEOUT_MS but execute_shell never
read it, so an operator cap was silently ignored. The model may now request
less than the cap, never more — and the effective timeout is in the evidence.
"""
from __future__ import annotations

from pathlib import Path

from test_shell_tool import make_settings

from nimna.tools.builtin.shell import ShellRequest, ShellStatus, execute_shell


def test_operator_cap_clamps_a_greedy_request(tmp_path):
    settings = make_settings(tmp_path)
    settings.shell_timeout_ms = 500
    result = execute_shell(
        ShellRequest(command="echo hi", timeout_ms=60_000),
        settings=settings, workspace_root=tmp_path, explicit_consent=True,
    )
    assert result.status is ShellStatus.SUCCESS
    assert result.evidence["timeout_ms_requested"] == 60_000
    assert result.evidence["timeout_ms_effective"] == 500
    assert result.evidence["operator_timeout_cap_ms"] == 500


def test_model_may_ask_for_less_than_the_cap(tmp_path):
    settings = make_settings(tmp_path)
    settings.shell_timeout_ms = 10_000
    result = execute_shell(
        ShellRequest(command="echo hi", timeout_ms=2_000),
        settings=settings, workspace_root=tmp_path, explicit_consent=True,
    )
    assert result.status is ShellStatus.SUCCESS
    assert result.evidence["timeout_ms_effective"] == 2_000


def test_non_positive_cap_is_ignored_not_bricking(tmp_path):
    settings = make_settings(tmp_path)
    settings.shell_timeout_ms = 0
    result = execute_shell(
        ShellRequest(command="echo hi", timeout_ms=3_000),
        settings=settings, workspace_root=tmp_path, explicit_consent=True,
    )
    assert result.status is ShellStatus.SUCCESS
    assert result.evidence["timeout_ms_effective"] == 3_000


def test_timeout_reason_reports_the_effective_timeout(tmp_path):
    settings = make_settings(tmp_path)
    settings.shell_timeout_ms = 300
    result = execute_shell(
        ShellRequest(command="sleep 5", timeout_ms=60_000),
        settings=settings, workspace_root=tmp_path, explicit_consent=True,
    )
    assert result.status is ShellStatus.TIMEOUT
    assert "300 ms" in result.reason


def test_default_cap_matches_settings_default(tmp_path):
    settings = make_settings(tmp_path)  # shell_timeout_ms untouched
    assert settings.shell_timeout_ms == 10_000
    result = execute_shell(
        ShellRequest(command="echo hi", timeout_ms=60_000),
        settings=settings, workspace_root=tmp_path, explicit_consent=True,
    )
    assert result.evidence["timeout_ms_effective"] == 10_000
