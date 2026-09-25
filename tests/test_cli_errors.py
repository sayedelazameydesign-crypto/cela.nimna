"""CLI boot failures are operator errors: one friendly line, exit 2, no traceback.

Regression tests: `nimna ask/chat/approvals` used to let ProviderError /
CostPolicyError escape as a raw traceback. `nimna serve` already had the
friendly contract — these tests pin the same contract on the other commands.
"""
from __future__ import annotations

import argparse

import pytest


def _args(**overrides):
    base = {
        "env_file": None, "provider": None, "model": None, "no_verify": False,
        "verbose": False, "message": "hi", "session": "test",
        "approvals_cmd": "list",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("NIMNA_ENV", "development")
    monkeypatch.setenv("DB_PATH", ":memory:")
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
                "NVIDIA_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GEMINI_FREE_TIER_MODELS", raising=False)


def test_ask_without_key_is_friendly_and_exit_2(no_keys, capsys):
    from nimna.cli import cmd_ask

    assert cmd_ask(_args()) == 2
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    assert "GEMINI_API_KEY" in err


def test_chat_without_key_is_friendly_and_exit_2(no_keys, capsys):
    from nimna.cli import cmd_chat

    assert cmd_chat(_args()) == 2
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    assert "GEMINI_API_KEY" in err


def test_approvals_without_key_is_friendly_and_exit_2(no_keys, capsys):
    from nimna.cli import cmd_approvals

    assert cmd_approvals(_args()) == 2
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    assert "GEMINI_API_KEY" in err


def test_cost_guard_refusal_is_friendly_and_exit_2(monkeypatch, capsys):
    """An unknown-price model under MAX_SPEND_USD=0 refuses to boot loudly."""
    from nimna.cli import cmd_ask

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.test-key-not-real")
    monkeypatch.setenv("GEMINI_MODEL", "some-unpriced-model-2099")
    monkeypatch.setenv("GEMINI_FREE_TIER_MODELS", "gemini-2.5-flash")
    monkeypatch.setenv("MAX_SPEND_USD", "0")
    monkeypatch.setenv("COST_GUARD_HARD", "true")
    monkeypatch.setenv("NIMNA_ENV", "development")
    monkeypatch.setenv("DB_PATH", ":memory:")
    assert cmd_ask(_args()) == 2
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    assert "nimna ask: cannot start" in err
