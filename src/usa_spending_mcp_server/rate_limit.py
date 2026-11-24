"""Global rate limiting for USAspending API calls.

Token bucket capped at 800 requests per 300-second window (safely below
USAspending's 1,000 req / 300s limit) with exponential backoff on
HTTP 429 / 503 / 504. Fail-closed: exhausted retries re-raise.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

RETRYABLE_STATUS_CODES = (429, 503, 504)


class AsyncTokenBucket:
    """Async token bucket: capacity tokens refill uniformly over window."""

    def __init__(self, capacity: int = 800, window_seconds: int = 300) -> None:
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(float(self.capacity), self._tokens + elapsed * (self.capacity / self.window_seconds))

    async def acquire(self, n: int = 1) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit * (self.window_seconds / self.capacity)
            await asyncio.sleep(min(wait, 5.0))


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    text = str(exc)
    for code in RETRYABLE_STATUS_CODES:
        if f"HTTP {code}" in text:
            return code
    return None


async def with_backoff(
    fn: Callable[[], Awaitable[Any]],
    *,
    retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Any:
    """Run fn with exponential backoff on retryable failures.

    Retries HTTP 429/503/504 plus transport-level httpx.RequestError
    (timeouts, disconnects); everything else re-raises immediately.
    Honors the server's Retry-After header when present and adds uniform
    jitter so concurrent workers do not retry in lockstep (egress-throttle
    hardening for long audit runs).
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - inspected for retryability
            retryable = (_status_of(exc) in RETRYABLE_STATUS_CODES
                         or isinstance(exc, httpx.RequestError)
                         or bool(getattr(exc, "transient", False)))
            if not retryable or attempt >= retries:
                raise
            server_wait = getattr(exc, "retry_after", None)
            ceiling = min(max_delay, base_delay * (2 ** attempt))
            status = _status_of(exc)
            if status == 429:
                # Fix 4 (strict): 429 triggers a 60s cooldown-and-retry so a
                # long audit backs off as a whole instead of hammering.
                delay = max(60.0, server_wait if isinstance(server_wait, (int, float)) else 0.0)
            elif isinstance(server_wait, (int, float)):
                delay = min(max_delay, max(ceiling, server_wait))
            else:
                delay = random.uniform(0, ceiling)
            await asyncio.sleep(delay)
            attempt += 1


# Shared process-wide bucket for all C-SCRM USAspending traffic.
global_bucket = AsyncTokenBucket(capacity=800, window_seconds=300)
