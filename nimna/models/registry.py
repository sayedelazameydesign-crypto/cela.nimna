"""Capability-aware model registry and hard cost guard.

The agent core should not need to know vendor-specific model names.  This module
keeps model selection deterministic and makes the zero-spend policy executable,
not just a README promise.

A cost profile with ``None`` prices is *unknown*.  In hard mode an unknown
profile is rejected when the configured budget is zero; this is intentional so
that a newly added provider cannot silently turn a free deployment into a paid
one.

Two layers enforce that:

* request time — :meth:`CostGuard.authorize` raises :class:`BudgetExceededError`;
* boot time — :meth:`CostGuard.from_settings` raises :class:`CostPolicyError`
  when the configured guard could never authorize *any* request (paid or
  undeclared model with ``MAX_SPEND_USD=0``).  Refusing to start is the same
  fail-closed contract already applied to a missing ``NIMNA_API_KEY`` or
  ``GEMINI_API_KEY``: a healthy-looking ``/api/health`` must not hide a runtime
  that blocks 100% of model calls.

"Free" is a *declaration*, not a discovery: only Gemini models listed in
``GEMINI_FREE_TIER_MODELS`` (default: ``gemini-2.5-flash``) are zero-cost
without explicit prices.  Swapping ``GEMINI_MODEL`` to anything else therefore
requires either extending that list or budgeting the model explicitly.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..config import DEFAULT_GEMINI_FREE_TIER_MODELS


class BudgetExceededError(RuntimeError):
    """Raised before a model request that cannot fit the configured budget."""


class CostPolicyError(RuntimeError):
    """Raised at boot when the configured cost policy could never authorize a request.

    A configuration error, not a runtime budget event: the process refuses to
    start instead of serving an endpoint whose every model call is blocked.
    """


def normalise_model_id(model_id: Any) -> str:
    """Canonical form for comparing model ids (``models/Gemini-2.5-Flash `` -> ``gemini-2.5-flash``)."""
    text = str(model_id or "").strip().lower()
    if text.startswith("models/"):
        text = text[len("models/"):]
    return text


def declared_free_tier_models(settings: Any) -> frozenset[str]:
    """Gemini model ids this deployment declares zero-cost (see ``GEMINI_FREE_TIER_MODELS``)."""
    declared = getattr(settings, "gemini_free_tier_models", None)
    if declared is None:
        declared = DEFAULT_GEMINI_FREE_TIER_MODELS
    return frozenset(item for item in (normalise_model_id(raw) for raw in declared) if item)


@dataclass(frozen=True)
class CostProfile:
    """Price in USD per 1,000 input/output tokens.

    ``None`` means that the price is not known by this repository.  A free-tier
    profile is still marked explicitly with ``known=True`` so it is distinguishable
    from an accidental missing price.
    """

    input_usd_per_1k: float | None = None
    output_usd_per_1k: float | None = None
    known: bool = False
    free_tier: bool = False

    def __post_init__(self) -> None:
        for value in (self.input_usd_per_1k, self.output_usd_per_1k):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("model prices must be finite and non-negative")

    @property
    def zero_cost(self) -> bool:
        return self.known and (self.input_usd_per_1k or 0.0) == 0.0 and (self.output_usd_per_1k or 0.0) == 0.0

    def estimate(self, input_tokens: int, output_tokens: int) -> float | None:
        if not self.known or self.input_usd_per_1k is None or self.output_usd_per_1k is None:
            return None
        return (max(0, input_tokens) / 1000.0) * self.input_usd_per_1k + (
            max(0, output_tokens) / 1000.0
        ) * self.output_usd_per_1k


@dataclass(frozen=True)
class LatencyProfile:
    """Relative latency hints used only for deterministic tie-breaking."""

    first_token_ms: int = 0
    tokens_per_second: float = 0.0


@dataclass(frozen=True)
class ModelProfile:
    id: str
    provider: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    context_window: int = 0
    tool_calling: bool = False
    vision: bool = False
    streaming: bool = False
    cost: CostProfile = field(default_factory=CostProfile)
    latency: LatencyProfile = field(default_factory=LatencyProfile)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def supports(self, required: Iterable[str]) -> bool:
        required_set = {str(item) for item in required}
        if "tool_calling" in required_set and not self.tool_calling:
            return False
        if "vision" in required_set and not self.vision:
            return False
        if "streaming" in required_set and not self.streaming:
            return False
        return required_set.difference({"tool_calling", "vision", "streaming"}).issubset(self.capabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "capabilities": sorted(self.capabilities),
            "context_window": self.context_window,
            "tool_calling": self.tool_calling,
            "vision": self.vision,
            "streaming": self.streaming,
            "cost": {
                "input_usd_per_1k": self.cost.input_usd_per_1k,
                "output_usd_per_1k": self.cost.output_usd_per_1k,
                "known": self.cost.known,
                "free_tier": self.cost.free_tier,
                "zero_cost": self.cost.zero_cost,
            },
            "latency": {
                "first_token_ms": self.latency.first_token_ms,
                "tokens_per_second": self.latency.tokens_per_second,
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ModelRequirements:
    capabilities: frozenset[str] = field(default_factory=frozenset)
    min_context_window: int = 0
    require_tool_calling: bool = False
    require_vision: bool = False
    require_streaming: bool = False

    def required_capabilities(self) -> frozenset[str]:
        values = set(self.capabilities)
        if self.require_tool_calling:
            values.add("tool_calling")
        if self.require_vision:
            values.add("vision")
        if self.require_streaming:
            values.add("streaming")
        return frozenset(values)


@dataclass(frozen=True)
class ModelSelection:
    profile: ModelProfile
    reason: str


class ModelRegistry:
    """Small in-process registry; a persistent registry can implement this contract later."""

    def __init__(self, profiles: Iterable[ModelProfile] | None = None):
        self._profiles: dict[str, ModelProfile] = {}
        for profile in profiles or ():
            self.register(profile)

    def register(self, profile: ModelProfile, *, replace: bool = False) -> ModelProfile:
        if profile.id in self._profiles and not replace:
            raise ValueError(f"model '{profile.id}' already registered")
        self._profiles[profile.id] = profile
        return profile

    def get(self, model_id: str) -> ModelProfile | None:
        return self._profiles.get(model_id)

    def list(self) -> list[ModelProfile]:
        return list(self._profiles.values())

    def select(
        self,
        requirements: ModelRequirements,
        *,
        max_cost_usd: float | None = None,
        preferred_provider: str | None = None,
    ) -> ModelSelection:
        required = requirements.required_capabilities()
        candidates = [
            profile
            for profile in self._profiles.values()
            if profile.context_window >= requirements.min_context_window
            and profile.supports(required)
            and (preferred_provider is None or profile.provider == preferred_provider)
        ]
        if max_cost_usd is not None:
            candidates = [
                p
                for p in candidates
                if p.cost.zero_cost or (p.cost.known and (p.cost.input_usd_per_1k or 0) <= max_cost_usd)
            ]
        if not candidates:
            raise LookupError("no registered model satisfies the requested capabilities and budget")
        # Free/cheaper models first, then lower latency, then stable id.
        candidates.sort(
            key=lambda p: (
                0 if p.cost.zero_cost else 1,
                p.cost.input_usd_per_1k if p.cost.input_usd_per_1k is not None else float("inf"),
                p.latency.first_token_ms or float("inf"),
                p.id,
            )
        )
        chosen = candidates[0]
        return ModelSelection(chosen, f"capability match; provider={chosen.provider}")

    def describe(self) -> list[dict[str, Any]]:
        return [profile.to_dict() for profile in self._profiles.values()]

    @classmethod
    def for_settings(cls, settings: Any, provider: Mapping[str, Any] | None = None) -> ModelRegistry:
        """Build the active entry from settings.

        Additional providers can register profiles without changing the Agent
        loop. Gemini's configured free-tier profile is deliberately explicit and
        *model-scoped*: only ids declared in ``GEMINI_FREE_TIER_MODELS`` are
        zero-cost without explicit prices; any other Gemini model is unknown
        pricing and fails closed under a zero budget.  It is not a claim that
        every Google account is free forever; quotas and billing still belong
        to the provider account.
        """

        provider_info = dict(provider or {})
        kind = str(provider_info.get("provider") or getattr(settings, "provider", "mock")).lower()
        model_id = str(provider_info.get("model") or getattr(settings, "model_name", "mock"))
        input_price = getattr(settings, "model_cost_input_usd_per_1k", None)
        output_price = getattr(settings, "model_cost_output_usd_per_1k", None)

        if kind == "mock" and input_price is None and output_price is None:
            cost = CostProfile(0.0, 0.0, known=True)
        elif kind == "gemini" and input_price is None and output_price is None:
            if normalise_model_id(model_id) in declared_free_tier_models(settings):
                cost = CostProfile(0.0, 0.0, known=True, free_tier=True)
            else:
                # A Gemini model this deployment has not declared free-tier is
                # unknown pricing: CostGuard refuses it under MAX_SPEND_USD=0.
                cost = CostProfile(known=False)
        elif input_price is not None and output_price is not None:
            cost = CostProfile(float(input_price), float(output_price), known=True)
        else:
            # Unknown paid-provider pricing must fail closed under a zero budget.
            cost = CostProfile(known=False)

        capabilities = {"text", "json", "tool_calling"}
        profile = ModelProfile(
            id=model_id,
            provider=kind,
            capabilities=frozenset(capabilities),
            context_window=int(getattr(settings, "model_context_window", 0) or 0),
            tool_calling=True,
            vision=kind == "gemini",
            streaming=False,
            cost=cost,
            latency=LatencyProfile(),
            metadata={"configured": True},
        )
        return cls([profile])


@dataclass(frozen=True)
class BudgetReservation:
    estimate_usd: float
    token_budget: int


class CostGuard:
    """Hard request budget with reservation/settlement semantics."""

    def __init__(
        self,
        profile: ModelProfile,
        *,
        max_spend_usd: float = 0.0,
        enabled: bool = True,
        hard: bool = True,
    ):
        if max_spend_usd < 0 or not math.isfinite(max_spend_usd):
            raise ValueError("max_spend_usd must be a finite non-negative value")
        self.profile = profile
        self.max_spend_usd = float(max_spend_usd)
        self.enabled = bool(enabled)
        self.hard = bool(hard)
        self.spent_usd = 0.0
        self._reserved_usd = 0.0
        self.request_count = 0
        self.blocked_count = 0

    @classmethod
    def from_settings(cls, settings: Any, provider: Mapping[str, Any] | None = None) -> CostGuard:
        """Build the guard from runtime settings and refuse a guard that blocks everything.

        Raises :class:`CostPolicyError` (boot refusal) — see :meth:`assert_boot_policy`.
        Direct construction (``CostGuard(profile, ...)``) is *not* gated so tests
        and tooling can still build deliberately blocking guards.
        """
        registry = ModelRegistry.for_settings(settings, provider)
        profile = registry.list()[0]
        guard = cls(
            profile,
            max_spend_usd=float(getattr(settings, "max_spend_usd", 0.0)),
            enabled=bool(getattr(settings, "cost_guard_enabled", True)),
            hard=bool(getattr(settings, "cost_guard_hard", True)),
        )
        guard.assert_boot_policy()
        return guard

    def boot_policy_violation(self) -> str | None:
        """Why this guard could never authorize a request — or ``None`` if it can.

        Pure (no counters touched).  Mirrors :meth:`authorize` for the smallest
        possible request: with a zero budget only a declared zero-cost profile
        passes; unknown pricing passes only in soft mode (metered as $0).
        """
        if not self.enabled:
            return None
        cost = self.profile.cost
        if cost.zero_cost or self.max_spend_usd > 0:
            return None
        who = f"model '{self.profile.id}' (provider '{self.profile.provider}')"
        if not cost.known:
            if not self.hard:
                return None
            return f"{who} has unknown pricing and MAX_SPEND_USD={self.max_spend_usd:g} in hard mode"
        return (
            f"{who} is priced ${cost.input_usd_per_1k:g} in / ${cost.output_usd_per_1k:g} out "
            f"per 1K tokens and MAX_SPEND_USD={self.max_spend_usd:g}"
        )

    def assert_boot_policy(self) -> None:
        """Raise :class:`CostPolicyError` when every model request would be blocked."""
        reason = self.boot_policy_violation()
        if reason is None:
            return
        if self.profile.provider == "gemini" and not self.profile.cost.known:
            remedy = (
                f"Either declare it free-tier on your account with "
                f"GEMINI_FREE_TIER_MODELS={self.profile.id} (comma-separated list; default "
                f"{','.join(DEFAULT_GEMINI_FREE_TIER_MODELS)}), or set MODEL_COST_INPUT_USD_PER_1K / "
                "MODEL_COST_OUTPUT_USD_PER_1K together with a positive MAX_SPEND_USD."
            )
        elif not self.profile.cost.known:
            remedy = (
                "Set MODEL_COST_INPUT_USD_PER_1K / MODEL_COST_OUTPUT_USD_PER_1K together with a "
                "positive MAX_SPEND_USD, or switch to a declared zero-cost model."
            )
        else:
            remedy = "Set a positive MAX_SPEND_USD, or switch to a declared zero-cost model."
        raise CostPolicyError(
            f"cost guard would block every model request: {reason}. "
            f"Refusing to start a runtime that cannot serve a single model call. {remedy}"
        )

    @staticmethod
    def _estimate_tokens(messages: Iterable[Any], max_output_tokens: int) -> tuple[int, int]:
        input_chars = 0
        for message in messages:
            input_chars += len(str(getattr(message, "content", "") or ""))
            for image in getattr(message, "images", []) or []:
                # Images are intentionally expensive/unknown to price here; the
                # caller can provide a larger explicit budget if using a paid tier.
                input_chars += len(str(image.get("data", ""))) // 4
        # Conservative enough for a gate, without requiring a tokenizer dependency.
        input_tokens = max(1, math.ceil(input_chars / 4))
        return input_tokens, max(0, int(max_output_tokens))

    def authorize(self, messages: Iterable[Any], max_output_tokens: int = 0) -> BudgetReservation:
        if not self.enabled:
            return BudgetReservation(0.0, 0)
        input_tokens, output_tokens = self._estimate_tokens(messages, max_output_tokens)
        estimate = self.profile.cost.estimate(input_tokens, output_tokens)
        if estimate is None:
            self.blocked_count += 1
            if self.hard:
                raise BudgetExceededError(
                    f"cost guard blocked model '{self.profile.id}': pricing is unknown and "
                    f"MAX_SPEND_USD={self.max_spend_usd:g}"
                )
            estimate = 0.0
        if self.spent_usd + self._reserved_usd + estimate > self.max_spend_usd + 1e-12:
            self.blocked_count += 1
            raise BudgetExceededError(
                f"cost guard blocked model '{self.profile.id}': estimated ${estimate:.6f} "
                f"would exceed remaining budget ${self.remaining_usd:.6f}"
            )
        self._reserved_usd += estimate
        self.request_count += 1
        return BudgetReservation(estimate, output_tokens)

    def settle(self, reservation: BudgetReservation, usage: Mapping[str, Any] | None = None) -> float:
        """Release a reservation and record actual usage when prices are known."""
        self._reserved_usd = max(0.0, self._reserved_usd - reservation.estimate_usd)
        actual = reservation.estimate_usd
        if self.profile.cost.known and usage:
            input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            measured = self.profile.cost.estimate(input_tokens, output_tokens)
            if measured is not None:
                actual = measured
        self.spent_usd += max(0.0, actual)
        return actual

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_spend_usd - self.spent_usd - self._reserved_usd)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": "hard" if self.hard else "soft",
            "max_spend_usd": self.max_spend_usd,
            "spent_usd": round(self.spent_usd, 9),
            "reserved_usd": round(self._reserved_usd, 9),
            "remaining_usd": round(self.remaining_usd, 9),
            "request_count": self.request_count,
            "blocked_count": self.blocked_count,
            "model": self.profile.id,
            "provider": self.profile.provider,
            "pricing_known": self.profile.cost.known,
            "zero_cost_profile": self.profile.cost.zero_cost,
        }


__all__ = [
    "BudgetExceededError",
    "BudgetReservation",
    "CostGuard",
    "CostPolicyError",
    "CostProfile",
    "LatencyProfile",
    "ModelProfile",
    "ModelRegistry",
    "ModelRequirements",
    "ModelSelection",
    "declared_free_tier_models",
    "normalise_model_id",
]
