import re
from typing import Any
from urllib.parse import urlsplit

import httpx


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds form)."""
    if not value:
        return None
    try:
        secs = float(value.strip())
    except ValueError:
        return None
    if secs < 0 or secs > 600:
        return None
    return secs


class USASpendingAPIError(Exception):
    """Caller-safe error raised when the USAspending API request fails."""

    def __init__(self, message: str, status_code: int | None = None,
                 retry_after: float | None = None,
                 transient: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        # Transport-level failures (timeouts, disconnects, RSTs) wrapped here
        # must stay retryable after wrapping — with_backoff honors this flag.
        self.transient = transient


class USASpendingClient:
    """Shared HTTP client for USA Spending API"""

    BASE_URL = "https://api.usaspending.gov/api/v2"

    def __init__(self, timeout: float = 30.0):
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json"},
            follow_redirects=False,
        )

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict[str, Any]:
        """Make HTTP request with unified error handling"""
        parsed_endpoint = urlsplit(endpoint)
        if (
            parsed_endpoint.scheme
            or parsed_endpoint.netloc
            or parsed_endpoint.query
            or parsed_endpoint.fragment
            or "\\" in endpoint
            or not re.fullmatch(r"/?[A-Za-z0-9_/-]+", endpoint)
            or any(segment in ("", ".", "..") for segment in endpoint.strip("/").split("/"))
        ):
            raise ValueError("Endpoint must be a normalized relative API path")

        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"

        try:
            response = await self.client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            detail = f"USAspending API request failed (HTTP {e.response.status_code})"
            if 300 <= e.response.status_code < 400:
                # Redirects surface here only when there is no followable
                # Location (follow_redirects=True is set on GETs). Capture
                # the raw response so the egress path can be diagnosed.
                loc = e.response.headers.get("location", "")
                try:
                    body = " ".join(e.response.text.split())[:150]
                except Exception:  # noqa: BLE001 - diagnostic only
                    body = ""
                detail += f" location={loc!r} body={body!r}"
            raise USASpendingAPIError(
                detail,
                status_code=e.response.status_code,
                retry_after=_retry_after_seconds(e.response.headers.get("retry-after")),
            ) from e
        except httpx.RequestError as e:
            raise USASpendingAPIError(
                f"USAspending API request failed ({type(e).__name__})",
                transient=True,
            ) from e

    async def post(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        """Make a POST request to the API"""
        return await self._request("POST", endpoint, json=data)

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request to the API"""
        # NOTE: follow redirects on GET — USAspending 301s slashless GETs and
        # the Databricks Apps egress path surfaces 301s even on slashed URLs.
        # The no-redirect default turned those into hard per-vehicle failures.
        return await self._request("GET", endpoint, params=params, follow_redirects=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.client.aclose()
