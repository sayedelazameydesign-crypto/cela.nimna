#!/usr/bin/env python3
"""Live provider proof — real, non-mock inference with falsifiable assertions.

Why this exists
---------------
Every offline test in this repository runs on ``MockProvider``.  That proves the
plumbing (tools, memory, approval gate, API) but says nothing about whether a
real model actually answers.  This script is the missing half.

Design rules (deliberate, do not relax)
---------------------------------------
1. Running it under ``MODEL_PROVIDER=mock`` is a **hard error**.  A mock run must
   never be mistakable for a live proof — the same honesty rule the arena suite
   applies when it stamps rows ``MOCKED``.
2. Every check is an assertion that exits non-zero, so the proof cannot pass by
   accident and cannot be masked in CI (see ``scripts/check_ci_falsifiable.sh``).
3. The API key is never printed.  Only which env var supplied it is reported.

Usage
-----
    MODEL_PROVIDER=gemini GEMINI_API_KEY=... python scripts/live_provider_proof.py

Exit codes
----------
    0  real inference proven
    1  proof failed (bad credential, network, or a mock-looking answer)
    2  misconfigured (no credential, or provider explicitly set to mock)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# The exact banner MockProvider emits.  If it appears in a "live" answer, the
# answer is not real and the proof must fail.
MOCK_BANNER = "(mock provider"

results: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> bool:
    results.append((label, passed, detail))
    icon = "PASS" if passed else "FAIL"
    print(f"[{icon}] {label}" + (f" — {detail}" if detail else ""))
    return passed


def main() -> int:
    from nimna.config import Settings
    from nimna.providers import create_provider, ProviderError
    from nimna.providers.base import Message

    settings = Settings.from_env()
    kind = (settings.provider or "").lower()

    print("=" * 72)
    print("Nimna live provider proof — real inference, no mocks")
    print("=" * 72)

    # -- rule 1: refuse to certify a mock run -------------------------------
    if kind == "mock":
        check("provider is not mock", False,
              "MODEL_PROVIDER=mock — plumbing proof only, NOT an inference proof")
        return 2

    # -- credential ---------------------------------------------------------
    key = settings.gemini_api_key if kind == "gemini" else settings.openai_api_key
    source = settings.gemini_key_source if kind == "gemini" else settings.openai_key_source
    if not check("credential present", bool(key),
                 f"supplied by {source}" if key else "set GEMINI_API_KEY (free tier, no billing)"):
        return 2

    print(f"       provider={kind}  model={settings.model_name}  key_source={source}")
    print("       (key value is never printed)")

    # -- 1. direct provider call -------------------------------------------
    print("\n-- 1. direct provider call " + "-" * 46)
    try:
        provider = create_provider(settings)
    except ProviderError as exc:
        check("provider constructed", False, str(exc)[:200])
        return 1
    check("provider constructed", True, f"{provider.name} / {provider.model}")

    t0 = time.perf_counter()
    try:
        resp = provider.generate(
            [Message.system("You are a proof harness. Answer with exactly one word."),
             Message.user("Reply with the single word: ACK")],
            max_tokens=16,
        )
    except Exception as exc:  # noqa: BLE001 - any failure means no proof
        check("live request completed", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        _summary()
        return 1
    latency_ms = int((time.perf_counter() - t0) * 1000)

    check("live request completed", True, f"{latency_ms} ms round-trip")
    text = (resp.text or "").strip()
    check("non-empty response", bool(text), repr(text[:80]))
    check("response is not the mock banner", MOCK_BANNER not in (resp.text or ""))
    tokens = int((resp.usage or {}).get("total_tokens") or 0)
    check("provider reported token usage", tokens > 0, f"total_tokens={tokens}")
    print(f"       answer: {text[:120]!r}")

    # -- 2. full agent loop -------------------------------------------------
    print("\n-- 2. full agent loop (real model, real tools) " + "-" * 25)
    from nimna.bootstrap import build_agent

    try:
        agent = build_agent(settings)
        result = agent.run("Reply with exactly: PROOF-OK", session_id="live-proof")
    except Exception as exc:  # noqa: BLE001
        check("agent run completed", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        _summary()
        return 1

    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    check("agent run completed", status == "done", f"status={status}")
    check("agent produced a reply", bool(result.reply), repr((result.reply or "")[:80]))
    check("agent reply is not the mock banner", MOCK_BANNER not in (result.reply or ""))
    agent_tokens = int((result.usage or {}).get("total_tokens") or 0)
    check("agent reported token usage", agent_tokens > 0, f"total_tokens={agent_tokens}")
    print(f"       steps={result.steps} skills={result.skills_used} run_id={result.run_id}")
    print(f"       reply: {(result.reply or '')[:200]!r}")

    # -- evidence -----------------------------------------------------------
    evidence = {
        "provider": provider.name,
        "model": provider.model,
        "key_source": source,
        "direct_call": {"latency_ms": latency_ms, "total_tokens": tokens, "answer": text[:200]},
        "agent_loop": {
            "status": status,
            "steps": result.steps,
            "skills_used": result.skills_used,
            "total_tokens": agent_tokens,
            "run_id": result.run_id,
            "reply": (result.reply or "")[:200],
        },
    }
    print("\n-- evidence (paste into docs/evidence) " + "-" * 33)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))

    return 0 if _summary() else 1


def _summary() -> bool:
    failed = [label for label, ok, _ in results if not ok]
    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    if failed:
        print(f"LIVE PROOF FAILED — {passed}/{len(results)} checks passed")
        for label in failed:
            print(f"  - {label}")
    else:
        print(f"LIVE PROOF OK — {passed}/{len(results)} checks passed "
              f"(real model, non-mock response)")
    print("=" * 72)
    return not failed


if __name__ == "__main__":
    sys.exit(main())
