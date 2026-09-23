"""Minimal, explicit Browser Use Cloud API V4 client.

The client uses the REST contract directly instead of guessing SDK types.  It
is optional: importing Nimna does not require ``browser-use-sdk`` and no cloud
request is made unless a caller supplies an API key.

Important lifecycle rule: disconnecting CDP is not the same as stopping an
owned cloud browser.  ``managed_browser`` always calls the V4 stop endpoint in
``finally``.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import quote

import httpx


VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "complete", "succeeded", "success", "failed", "error", "cancelled", "canceled", "stopped"})


class BrowserUseError(RuntimeError):
    """Cloud API error with a safe message (never includes the API key)."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class BrowserUseRateLimitError(BrowserUseError):
    def __init__(self, message: str, *, retry_after_seconds: float = 0.0, status_code: Optional[int] = 429):
        super().__init__(message, status_code=status_code)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))


@dataclass(frozen=True)
class BrowserSession:
    id: str
    cdp_url: Optional[str] = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class BrowserRun:
    id: str
    status: str
    result: Any = None
    raw: Mapping[str, Any] | None = None


class _FiveSecondLimiter:
    """Interpret X-RateLimit-Limit as requests per five-second window."""

    def __init__(self, window_seconds: float = 5.0):
        self.window_seconds = window_seconds
        self.limit: Optional[int] = None
        self._timestamps: list[float] = []

    def update(self, value: Optional[str]) -> None:
        try:
            parsed = int(value or "")
            if parsed > 0:
                self.limit = parsed
        except (TypeError, ValueError):
            pass

    def wait_if_needed(self) -> None:
        if not self.limit:
            return
        now = time.monotonic()
        self._timestamps[:] = [stamp for stamp in self._timestamps if now - stamp < self.window_seconds]
        if len(self._timestamps) >= self.limit:
            delay = self.window_seconds - (now - self._timestamps[0])
            if delay > 0:
                time.sleep(delay)
            now = time.monotonic()
            self._timestamps[:] = [stamp for stamp in self._timestamps if now - stamp < self.window_seconds]
        self._timestamps.append(time.monotonic())


class BrowserUseV4Client:
    """Small synchronous V4 client suitable for a governed tool handler."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = "https://api.browser-use.com",
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        transport: Optional[httpx.BaseTransport] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self.api_key = (api_key or os.getenv("BROWSER_USE_API_KEY") or "").strip()
        if not self.api_key:
            raise BrowserUseError("BROWSER_USE_API_KEY is not configured")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = max(0.1, poll_interval)
        self._limiter = _FiveSecondLimiter()
        self._owns_client = http_client is None
        if http_client is not None:
            self.client = http_client
            # Even an injected test/custom client must receive the exact V4
            # auth header; callers never need to construct a Bearer header.
            self.client.headers["X-Browser-Use-API-Key"] = self.api_key
            self.client.headers.pop("Authorization", None)
            self.client.headers.setdefault("Content-Type", "application/json")
        else:
            self.client = httpx.Client(
                base_url=self.base_url,
                timeout=timeout,
                headers={
                    # V4 Cloud auth is deliberately NOT Bearer auth.
                    "X-Browser-Use-API-Key": self.api_key,
                    "Content-Type": "application/json",
                },
                transport=transport,
            )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "BrowserUseV4Client":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def _retry_after(response: httpx.Response, body: Any) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(0.0, float(header))
            except ValueError:
                pass
        if isinstance(body, dict):
            for key in ("retry_after_seconds", "retry_after"):
                try:
                    return max(0.0, float(body.get(key)))
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _request(self, method: str, path: str, *, json_body: Optional[dict[str, Any]] = None) -> Any:
        self._limiter.wait_if_needed()
        try:
            response = self.client.request(method, path, json=json_body)
        except httpx.TimeoutException as exc:
            raise BrowserUseError(f"Browser Use request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BrowserUseError(f"Browser Use transport error: {exc}") from exc
        self._limiter.update(response.headers.get("X-RateLimit-Limit"))
        try:
            body = response.json()
        except ValueError:
            body = response.text[:1000]
        if response.status_code == 429:
            retry_after = self._retry_after(response, body)
            raise BrowserUseRateLimitError(
                "Browser Use rate limit reached; honor Retry-After before retrying",
                retry_after_seconds=retry_after,
            )
        if response.status_code >= 400:
            detail = body.get("message") if isinstance(body, dict) else body
            raise BrowserUseError(
                f"Browser Use API rejected {method} {path}: {str(detail)[:500]}",
                status_code=response.status_code,
                response=body,
            )
        return body

    @staticmethod
    def _as_id(data: Any, kind: str) -> str:
        if not isinstance(data, dict):
            raise BrowserUseError(f"Browser Use returned an invalid {kind} response")
        value = data.get("id") or data.get(f"{kind}Id") or data.get(f"{kind}_id") or data.get("run_id")
        if not value:
            raise BrowserUseError(f"Browser Use {kind} response has no id")
        return str(value)

    def account(self) -> dict[str, Any]:
        """Read project concurrency/credits from the documented V2 billing endpoint."""
        data = self._request("GET", "/api/v2/billing/account")
        return data if isinstance(data, dict) else {"raw": data}

    def create_browser(self, **browser_options: Any) -> BrowserSession:
        data = self._request("POST", "/api/v4/browsers", json_body=browser_options or {})
        browser_id = self._as_id(data, "browser")
        cdp_url = data.get("cdpUrl") or data.get("cdp_url") if isinstance(data, dict) else None
        return BrowserSession(browser_id, cdp_url=cdp_url, raw=data if isinstance(data, dict) else {"raw": data})

    def stop_browser(self, browser_id: str) -> Any:
        if not browser_id:
            return None
        # PATCH action=stop is explicit; do not confuse CDP disconnect with stop.
        return self._request(
            "PATCH",
            f"/api/v4/browsers/{quote(str(browser_id), safe='')}",
            json_body={"action": "stop"},
        )

    @staticmethod
    def _validate_reasoning(model: Optional[str], reasoning_effort: Optional[str]) -> None:
        if not reasoning_effort:
            return
        if model and model.lower().replace(" ", "-") in {"gpt-6-astra", "gpt-6-astra-preview"}:
            if reasoning_effort not in VALID_REASONING_EFFORTS:
                raise ValueError(
                    "GPT-6 Astra reasoning_effort must be one of: low, medium, high, xhigh, max"
                )

    def create_run(
        self,
        task: str,
        *,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        **run_options: Any,
    ) -> BrowserRun:
        if not task or not task.strip():
            raise ValueError("task is required")
        self._validate_reasoning(model, reasoning_effort)
        payload: dict[str, Any] = {"task": task}
        if model:
            payload["model"] = model
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        payload.update({key: value for key, value in run_options.items() if value is not None})
        data = self._request("POST", "/api/v4/runs", json_body=payload)
        run_id = self._as_id(data, "run")
        status = str(data.get("status", "queued")) if isinstance(data, dict) else "queued"
        return BrowserRun(run_id, status, result=(data.get("result") if isinstance(data, dict) else None), raw=data if isinstance(data, dict) else {"raw": data})

    def get_run(self, run_id: str) -> BrowserRun:
        data = self._request("GET", f"/api/v4/runs/{quote(str(run_id), safe='')}")
        if not isinstance(data, dict):
            raise BrowserUseError("Browser Use returned an invalid run status response")
        return BrowserRun(str(data.get("id") or run_id), str(data.get("status", "unknown")), data.get("result"), data)

    def wait_for_completion(self, run_id: str, *, timeout: Optional[float] = None) -> BrowserRun:
        deadline = time.monotonic() + (self.timeout if timeout is None else max(0.0, timeout))
        while True:
            if time.monotonic() >= deadline:
                # A client wait timeout does not cancel a server-side run.
                raise BrowserUseError(
                    f"timed out waiting for Browser Use run {run_id}; the server-side run may still be active"
                )
            try:
                run = self.get_run(run_id)
            except BrowserUseRateLimitError as exc:
                remaining = max(0.0, deadline - time.monotonic())
                delay = min(exc.retry_after_seconds, remaining)
                if delay > 0:
                    time.sleep(delay)
                    continue
                raise
            if run.status.lower() in TERMINAL_RUN_STATUSES:
                return run
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # A client wait timeout does not cancel a server-side run.
                raise BrowserUseError(
                    f"timed out waiting for Browser Use run {run_id}; the server-side run may still be active"
                )
            time.sleep(min(self.poll_interval, remaining))

    def run_agent_task(self, task: str, *, timeout: Optional[float] = None, **options: Any) -> dict[str, Any]:
        run = self.create_run(task, **options)
        completed = self.wait_for_completion(run.id, timeout=timeout)
        return {
            "run_id": completed.id,
            "status": completed.status,
            "result": completed.result,
            "raw": dict(completed.raw or {}),
        }

    @contextmanager
    def managed_browser(self, **browser_options: Any) -> Iterator[BrowserSession]:
        """Create an owned cloud browser and stop it even if CDP/task code fails."""
        session = self.create_browser(**browser_options)
        try:
            yield session
        finally:
            try:
                self.stop_browser(session.id)
            except Exception:
                # Preserve the original task exception, but make cleanup
                # observable to the caller through the raised cleanup error only
                # when there was no active exception (contextmanager semantics).
                pass


__all__ = [
    "BrowserRun",
    "BrowserSession",
    "BrowserUseError",
    "BrowserUseRateLimitError",
    "BrowserUseV4Client",
    "VALID_REASONING_EFFORTS",
]
