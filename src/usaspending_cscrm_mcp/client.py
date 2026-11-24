"""Resilient USAspending HTTP transport (§7).

- Shared httpx.AsyncClient, bounded pool, explicit timeouts.
- Requests restricted to https://api.usaspending.gov (plus separately
  allowlisted auth discovery/JWKS origins — never tool-caller URLs).
- Token bucket: 800 requests / 300 s refill, burst 20, max 8 concurrent.
  In-process scope (documented limitation for multi-worker deployments).
- Exception hierarchy with instance-level `is_transient`, preserved through
  wrapping. Retries: timeouts, disconnects, 429/502/503/504 with exponential
  backoff + jitter and finite budgets. 400/401/403/404/422 fail fast.
- IDV POST paths require a trailing slash before dispatch (ValueError).
- Redirects disabled: 301/302 surfaces as an error, never a silent GET.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

USASPENDING_BASE = "https://api.usaspending.gov"
API_V2 = USASPENDING_BASE + "/api/v2"

REFILL_REQUESTS = 800
REFILL_SECONDS = 300.0
BURST_CAPACITY = 20
MAX_CONCURRENCY = 8
MAX_ATTEMPTS = 4
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0

RETRYABLE_STATUS = {429, 502, 503, 504}
NONTRANSIENT_STATUS = {400, 401, 403, 404, 422}


class TransportError(Exception):
    """Base transport error with an instance-level transient flag."""

    def __init__(self, message: str, *, transient: bool,
                 endpoint: str = "", status: int | None = None,
                 attempts: int = 1) -> None:
        super().__init__(message)
        self.is_transient = transient
        self.endpoint = endpoint
        self.status = status
        self.attempts = attempts


class NonTransientError(TransportError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(message, transient=False, **kw)


def _is_transient_exc(exc: BaseException) -> bool:
    if isinstance(exc, TransportError):
        return exc.is_transient
    return isinstance(exc, (httpx.TimeoutException, httpx.RemoteProtocolError,
                            httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                            httpx.PoolTimeout))


class TokenBucket:
    """Async token bucket (in-process scope)."""

    def __init__(self, refill: int = REFILL_REQUESTS,
                 period_s: float = REFILL_SECONDS,
                 burst: int = BURST_CAPACITY) -> None:
        self._rate = refill / period_s
        self._burst = burst
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst,
                               self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            deficit = 1.0 - self._tokens
        await asyncio.sleep(deficit / self._rate)
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst,
                               self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            self._tokens = max(0.0, self._tokens - 1.0)


def parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class USAspendingClient:
    """Shared, rate-limited USAspending v2 client."""

    def __init__(self, timeout: float = 30.0,
                 max_concurrency: int = MAX_CONCURRENCY) -> None:
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json"},
            follow_redirects=False,
            limits=httpx.Limits(max_connections=max_concurrency,
                                max_keepalive_connections=max_concurrency),
        )
        self.bucket = TokenBucket()
        self.semaphore = asyncio.Semaphore(max_concurrency)

    async def aclose(self) -> None:
        await self.client.aclose()

    def _url(self, endpoint: str) -> str:
        if endpoint.startswith(("http://", "https://")):
            if not endpoint.startswith(USASPENDING_BASE):
                raise NonTransientError(
                    f"URL outside allowlist: {endpoint}", endpoint=endpoint)
            return endpoint
        return API_V2 + "/" + endpoint.lstrip("/")

    async def get(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        return await self._request("GET", endpoint, **kwargs)

    async def post(self, endpoint: str, payload: dict[str, Any] | None = None,
                   **kwargs: Any) -> dict[str, Any]:
        return await self._request("POST", endpoint, json=payload or {}, **kwargs)

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if method == "POST":
            require_trailing_slash(endpoint)
        url = self._url(endpoint)
        attempts = 0
        while True:
            attempts += 1
            await self.bucket.acquire()
            try:
                async with self.semaphore:
                    resp = await self.client.request(method, url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - classified below
                if _is_transient_exc(exc) and attempts < MAX_ATTEMPTS:
                    await self._backoff(attempts, None)
                    continue
                raise TransportError(f"{method} {endpoint} transport failure: {exc}",
                                     transient=_is_transient_exc(exc),
                                     endpoint=endpoint, attempts=attempts) from exc
            if resp.status_code in (301, 302, 307, 308):
                raise NonTransientError(
                    f"{method} {endpoint} redirected ({resp.status_code}); "
                    "redirects are disabled", endpoint=endpoint,
                    status=resp.status_code, attempts=attempts)
            if resp.status_code in NONTRANSIENT_STATUS:
                raise NonTransientError(
                    f"{method} {endpoint} -> HTTP {resp.status_code}",
                    endpoint=endpoint, status=resp.status_code, attempts=attempts)
            if resp.status_code in RETRYABLE_STATUS:
                if attempts < MAX_ATTEMPTS:
                    retry_after = parse_retry_after_seconds(
                        resp.headers.get("Retry-After"))
                    await self._backoff(attempts, retry_after)
                    continue
                raise TransportError(
                    f"{method} {endpoint} -> HTTP {resp.status_code} after "
                    f"{attempts} attempts", transient=True, endpoint=endpoint,
                    status=resp.status_code, attempts=attempts)
            if resp.status_code >= 400:
                raise NonTransientError(
                    f"{method} {endpoint} -> HTTP {resp.status_code}",
                    endpoint=endpoint, status=resp.status_code, attempts=attempts)
            try:
                data = resp.json()
            except ValueError as exc:
                raise NonTransientError(
                    f"{method} {endpoint}: non-JSON response",
                    endpoint=endpoint, status=resp.status_code,
                    attempts=attempts) from exc
            return data if isinstance(data, dict) else {"results": data}

    async def _backoff(self, attempts: int, retry_after: float | None) -> None:
        delay = min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** (attempts - 1)))
        delay = delay * (0.5 + random.random())
        if retry_after is not None:
            delay = min(MAX_BACKOFF_S, max(delay, retry_after))
        logger.info("Retry %d after %.1fs", attempts, delay)
        await asyncio.sleep(delay)


def require_trailing_slash(endpoint: str) -> None:
    """Validate-then-dispatch guard: IDV POST paths must end with '/'."""
    if endpoint.endswith("/"):
        return
    parts = endpoint.split("/")
    if len(parts) >= 2 and parts[-2] == "idvs" and parts[-1] in ("awards", "activity"):
        raise ValueError(f"IDV POST path requires trailing slash: {endpoint!r}")
