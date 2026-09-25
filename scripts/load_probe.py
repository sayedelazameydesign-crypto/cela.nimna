#!/usr/bin/env python3
"""Minimal load / smoke probe for a running Nimna server (stdlib only).

Hammers ``/api/health`` (always) and ``/api/chat`` (only with --include-chat)
with N concurrent workers, then reports p50/p95/max latency and the error
count.  Exits non-zero when thresholds are breached so CI or a runbook step
can gate on it.

SAFETY: ``/api/chat`` runs the real agent — against a live provider that
spends quota / calls tools.  Chat probing is therefore OFF by default and the
flag prints a warning.  Point this at mock-mode staging unless you know why.

    python scripts/load_probe.py --base-url http://127.0.0.1:8000 --requests 50 --concurrency 4
    NIMNA_API_KEY=... python scripts/load_probe.py --base-url https://... --include-chat --requests 20

Exit codes: 0 = within thresholds, 1 = probe error, 2 = threshold breach,
64 = usage error.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request


def _do_request(base_url: str, key: str, timeout: float, include_chat: bool, index: int) -> tuple[str, float, str]:
    started = time.monotonic()
    try:
        if include_chat and index % 2 == 1:
            payload = json.dumps({"message": "load probe ping", "session_id": f"probe-{index}"}).encode()
            req = urllib.request.Request(
                base_url + "/api/chat", data=payload,
                headers={"Content-Type": "application/json", "X-Nimna-Key": key}, method="POST")
        else:
            req = urllib.request.Request(base_url + "/api/health", headers={"X-Nimna-Key": key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        return ("http_error", time.monotonic() - started, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 — the probe reports, never raises
        return ("error", time.monotonic() - started, f"{exc.__class__.__name__}: {exc}")
    if status != 200:
        return ("http_error", time.monotonic() - started, f"HTTP {status}")
    return ("ok", time.monotonic() - started, "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load-probe a running Nimna server.")
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8000")
    parser.add_argument("--key", default=os.environ.get("NIMNA_API_KEY", ""),
                        help="API key (default: $NIMNA_API_KEY; health needs none)")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--include-chat", action="store_true",
                        help="also POST /api/chat (spends quota on live providers!)")
    parser.add_argument("--max-p95-ms", type=float, default=5000.0)
    parser.add_argument("--max-errors", type=int, default=0)
    args = parser.parse_args(argv)
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be >= 1")
    if args.include_chat:
        print("WARNING: --include-chat runs the real agent per probe request "
              "(quota/tools on live providers).", file=sys.stderr)
        if not args.key:
            print("load_probe: --include-chat needs --key (chat is authenticated)", file=sys.stderr)
            return 64
    base_url = args.base_url.rstrip("/")
    latencies: list[float] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(_do_request, base_url, args.key, args.timeout, args.include_chat, i)
                   for i in range(args.requests)]
        for future in concurrent.futures.as_completed(futures):
            outcome, elapsed, detail = future.result()
            latencies.append(elapsed * 1000.0)
            if outcome != "ok":
                errors.append(detail)
    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
    print(f"requests={args.requests} concurrency={args.concurrency} "
          f"errors={len(errors)} p50={p50:.0f}ms p95={p95:.0f}ms max={latencies[-1]:.0f}ms")
    for detail in sorted(set(errors))[:5]:
        print(f"  error: {detail}")
    if len(errors) > args.max_errors:
        print(f"BREACH: {len(errors)} errors > --max-errors {args.max_errors}", file=sys.stderr)
        return 2
    if p95 > args.max_p95_ms:
        print(f"BREACH: p95 {p95:.0f}ms > --max-p95-ms {args.max_p95_ms:.0f}ms", file=sys.stderr)
        return 2
    print("within thresholds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
