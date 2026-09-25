"""Boot-time cost policy: a paid or undeclared model with MAX_SPEND_USD=0 must
refuse to start, exactly like a missing NIMNA_API_KEY / GEMINI_API_KEY.

Background: the health endpoint reports ``zero_cost_profile: true`` for the
declared free-tier model.  Before this contract, *any* ``GEMINI_MODEL`` was
declared free and any paid provider was blocked only at request time — a
healthy-looking ``/api/health`` in front of a runtime that serves zero model
calls.  These tests pin the loud, early failure instead.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nimna.api.app import create_app
from nimna.config import DEFAULT_GEMINI_FREE_TIER_MODELS, Settings
from nimna.models import (
    BudgetExceededError,
    CostGuard,
    CostPolicyError,
    CostProfile,
    ModelProfile,
    ModelRegistry,
    normalise_model_id,
)

REPO = Path(__file__).resolve().parents[1]
FAKE_GEMINI_KEY = "test-gemini-key-not-real-0123456789"


def _settings(**overrides) -> SimpleNamespace:
    """Duck-typed settings, the way ModelRegistry/CostGuard consume them."""
    base = dict(
        provider="gemini",
        model_name="gemini-2.5-flash",
        max_spend_usd=0.0,
        cost_guard_enabled=True,
        cost_guard_hard=True,
        model_cost_input_usd_per_1k=None,
        model_cost_output_usd_per_1k=None,
        model_context_window=0,
        gemini_free_tier_models=list(DEFAULT_GEMINI_FREE_TIER_MODELS),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# declaration is model-scoped, not provider-scoped
# ---------------------------------------------------------------------------

def test_default_declaration_is_exactly_the_shipped_model():
    assert DEFAULT_GEMINI_FREE_TIER_MODELS == ("gemini-2.5-flash",)
    assert Settings.from_env(env_file=None).gemini_free_tier_models == ["gemini-2.5-flash"]


def test_declared_gemini_model_is_zero_cost():
    profile = ModelRegistry.for_settings(_settings()).list()[0]
    assert profile.cost.zero_cost is True
    assert profile.cost.free_tier is True
    assert profile.cost.known is True


def test_undeclared_gemini_model_is_unknown_pricing_not_free():
    """The trust boundary: swapping GEMINI_MODEL no longer inherits 'free'."""
    profile = ModelRegistry.for_settings(_settings(model_name="gemini-2.5-pro")).list()[0]
    assert profile.cost.known is False
    assert profile.cost.zero_cost is False


def test_extending_the_declaration_makes_the_model_free():
    settings = _settings(model_name="gemini-2.5-pro",
                         gemini_free_tier_models=["gemini-2.5-flash", "gemini-2.5-pro"])
    profile = ModelRegistry.for_settings(settings).list()[0]
    assert profile.cost.zero_cost is True


@pytest.mark.parametrize("model_id", ["models/gemini-2.5-flash", " Gemini-2.5-Flash ", "GEMINI-2.5-FLASH"])
def test_declaration_matching_normalises_prefix_case_and_whitespace(model_id):
    assert normalise_model_id(model_id) == "gemini-2.5-flash"
    profile = ModelRegistry.for_settings(_settings(model_name=model_id)).list()[0]
    assert profile.cost.zero_cost is True


def test_explicit_prices_override_the_declaration():
    settings = _settings(model_cost_input_usd_per_1k=0.001, model_cost_output_usd_per_1k=0.002)
    profile = ModelRegistry.for_settings(settings).list()[0]
    assert profile.cost.known is True and profile.cost.zero_cost is False


def test_mock_provider_stays_zero_cost_regardless_of_model_id():
    profile = ModelRegistry.for_settings(_settings(provider="mock", model_name="whatever")).list()[0]
    assert profile.cost.zero_cost is True


def test_env_list_parsing_for_free_tier_models(monkeypatch):
    monkeypatch.setenv("GEMINI_FREE_TIER_MODELS", " gemini-2.5-flash , gemini-2.5-pro ,, ")
    assert Settings.from_env(env_file=None).gemini_free_tier_models == ["gemini-2.5-flash", "gemini-2.5-pro"]
    monkeypatch.setenv("GEMINI_FREE_TIER_MODELS", "   ")
    assert Settings.from_env(env_file=None).gemini_free_tier_models == ["gemini-2.5-flash"]


# ---------------------------------------------------------------------------
# CostGuard.from_settings refuses a guard that would block everything
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label, overrides, needle",
    [
        ("undeclared gemini, hard, $0",
         dict(model_name="gemini-2.5-pro"), "GEMINI_FREE_TIER_MODELS=gemini-2.5-pro"),
        ("openai unknown pricing, hard, $0",
         dict(provider="openai", model_name="gpt-4o"), "MODEL_COST_INPUT_USD_PER_1K"),
        ("paid pricing, hard, $0",
         dict(provider="openai", model_name="gpt-4o",
              model_cost_input_usd_per_1k=0.005, model_cost_output_usd_per_1k=0.015), "positive MAX_SPEND_USD"),
        ("paid pricing, soft, $0 (authorize would still block)",
         dict(provider="openai", model_name="gpt-4o", cost_guard_hard=False,
              model_cost_input_usd_per_1k=0.005, model_cost_output_usd_per_1k=0.015), "positive MAX_SPEND_USD"),
    ],
)
def test_from_settings_refuses_all_blocking_guards(label, overrides, needle):
    with pytest.raises(CostPolicyError, match="would block every model request") as error:
        CostGuard.from_settings(_settings(**overrides))
    message = str(error.value)
    assert "Refusing to start" in message, label
    assert needle in message, label


@pytest.mark.parametrize(
    "label, overrides",
    [
        ("declared gemini, $0", dict()),
        ("mock, $0", dict(provider="mock", model_name="mock")),
        ("paid with explicit budget", dict(provider="openai", model_name="gpt-4o", max_spend_usd=1.0,
                                           model_cost_input_usd_per_1k=0.005, model_cost_output_usd_per_1k=0.015)),
        ("unknown pricing with explicit budget", dict(provider="openai", model_name="gpt-4o", max_spend_usd=1.0)),
        ("unknown pricing, soft mode (metered as $0 — unchanged semantics)",
         dict(provider="openai", model_name="gpt-4o", cost_guard_hard=False)),
        ("guard disabled", dict(provider="openai", model_name="gpt-4o", cost_guard_enabled=False)),
    ],
)
def test_from_settings_allows_serviceable_guards(label, overrides):
    guard = CostGuard.from_settings(_settings(**overrides))
    assert guard.boot_policy_violation() is None, label
    assert guard.status()["blocked_count"] == 0


def test_boot_policy_check_is_pure_and_agrees_with_authorize():
    """The predicate must not mutate counters and must match the real gate."""
    blocked = CostGuard(ModelProfile("future", "future", cost=CostProfile()), max_spend_usd=0)
    assert blocked.boot_policy_violation() is not None
    assert blocked.status()["blocked_count"] == 0 and blocked.status()["request_count"] == 0
    with pytest.raises(BudgetExceededError):
        blocked.authorize([], max_output_tokens=1)

    free = CostGuard(ModelProfile("mock", "mock", cost=CostProfile(0, 0, known=True)), max_spend_usd=0)
    assert free.boot_policy_violation() is None
    free.authorize([], max_output_tokens=1)  # does not raise


def test_direct_construction_is_not_gated():
    """Tests/tooling may still build a deliberately blocking guard."""
    guard = CostGuard(ModelProfile("future", "future", cost=CostProfile()), max_spend_usd=0)
    assert guard.boot_policy_violation() is not None  # but no exception at construction


# ---------------------------------------------------------------------------
# boot order: NIMNA_API_KEY -> GEMINI_API_KEY -> cost policy -> FastAPI()
# ---------------------------------------------------------------------------

def _production_gemini_env(monkeypatch, tmp_path: Path, model: str) -> None:
    monkeypatch.setenv("NIMNA_ENV", "production")
    monkeypatch.setenv("NIMNA_API_KEY", secrets.token_urlsafe(32))
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    monkeypatch.setenv("GEMINI_MODEL", model)
    monkeypatch.delenv("GEMINI_FREE_TIER_MODELS", raising=False)
    monkeypatch.delenv("MAX_SPEND_USD", raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("SKILLS_DIR", str(REPO / "skills"))


def test_boot_refuses_undeclared_model_before_constructing_fastapi(monkeypatch, tmp_path):
    """Secrets present, model swapped: CostPolicyError fires before FastAPI() exists."""
    built = {"n": 0}
    import nimna.api.app as appmod

    real = appmod.FastAPI

    def wrapped(*args, **kwargs):
        built["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(appmod, "FastAPI", wrapped)
    _production_gemini_env(monkeypatch, tmp_path, "gemini-2.5-pro")
    settings = Settings.from_env(env_file=None)
    with pytest.raises(CostPolicyError, match="gemini-2.5-pro"):
        create_app(settings)
    assert built["n"] == 0


def test_boot_proceeds_once_the_model_is_declared(monkeypatch, tmp_path):
    _production_gemini_env(monkeypatch, tmp_path, "gemini-2.5-pro")
    monkeypatch.setenv("GEMINI_FREE_TIER_MODELS", "gemini-2.5-flash,gemini-2.5-pro")
    app = create_app(Settings.from_env(env_file=None))
    status = app.state.agent.cost_guard.status()
    assert status["model"] == "gemini-2.5-pro"
    assert status["zero_cost_profile"] is True
    assert status["max_spend_usd"] == 0.0


def test_boot_proceeds_with_an_explicit_budget_instead(monkeypatch, tmp_path):
    _production_gemini_env(monkeypatch, tmp_path, "gemini-2.5-pro")
    monkeypatch.setenv("MODEL_COST_INPUT_USD_PER_1K", "0.00125")
    monkeypatch.setenv("MODEL_COST_OUTPUT_USD_PER_1K", "0.01")
    monkeypatch.setenv("MAX_SPEND_USD", "0.50")
    app = create_app(Settings.from_env(env_file=None))
    status = app.state.agent.cost_guard.status()
    assert status["zero_cost_profile"] is False and status["pricing_known"] is True
    assert status["remaining_usd"] == 0.5


def test_shipped_default_model_still_boots_under_zero_budget(monkeypatch, tmp_path):
    """Regression guard for the live deployment's exact configuration."""
    _production_gemini_env(monkeypatch, tmp_path, "gemini-2.5-flash")
    app = create_app(Settings.from_env(env_file=None))
    assert app.state.agent.cost_guard.status()["zero_cost_profile"] is True


def test_nimna_serve_exits_2_for_undeclared_model(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NIMNA_")}
    env.update(
        NIMNA_ENV="production", NIMNA_API_KEY=secrets.token_urlsafe(32),
        MODEL_PROVIDER="gemini", GEMINI_API_KEY=FAKE_GEMINI_KEY, GEMINI_MODEL="gemini-2.5-pro",
        DB_PATH=":memory:", WORKSPACE_DIR=str(tmp_path / "ws"), SKILLS_DIR=str(REPO / "skills"),
        PYTHONPATH=str(REPO),
    )
    env.pop("GEMINI_FREE_TIER_MODELS", None)
    env.pop("MAX_SPEND_USD", None)
    proc = subprocess.run([sys.executable, "-m", "nimna", "serve", "--port", "1"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2, proc.stderr
    assert "refusing to start" in proc.stderr
    assert "GEMINI_FREE_TIER_MODELS" in proc.stderr
    assert FAKE_GEMINI_KEY not in proc.stdout + proc.stderr
