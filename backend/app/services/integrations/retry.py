"""HTTP retry helper for Google APIs (never logs tokens)."""
from __future__ import annotations

import asyncio
import random
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 5
DEFAULT_BASE_DELAY = 1.0
MAX_DELAY = 60.0


class GoogleApiError(Exception):
    """Non-2xx response from a Google API."""

    def __init__(self, status: int, message: str, reason: str | None = None) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.reason = reason


def _parse_error(resp: httpx.Response) -> tuple[str, str | None]:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text[:500] or resp.reason_phrase), None
    if not isinstance(data, dict):
        return str(data)[:500], None
    err = data.get("error")
    if isinstance(err, dict):
        details = err.get("errors") or []
        reason = details[0].get("reason") if details and isinstance(details[0], dict) else None
        reason = reason or err.get("status")
        return str(err.get("message", "")), reason
    if isinstance(err, str):  # OAuth style
        return str(data.get("error_description") or err), err
    return resp.text[:500], None


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


def _backoff(attempt: int, base_delay: float) -> float:
    if base_delay <= 0:
        return 0.0
    return min(MAX_DELAY, base_delay * (2**attempt)) * (0.5 + random.random() / 2)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float | None = None,
    **kw: Any,
) -> httpx.Response:
    """Send a request, retrying transient failures with backoff; raise GoogleApiError on non-2xx."""
    delay_base = DEFAULT_BASE_DELAY if base_delay is None else base_delay
    for attempt in range(max_retries + 1):
        last = attempt == max_retries
        try:
            resp = await client.request(method, url, **kw)
        except httpx.TransportError as exc:
            if last:
                raise GoogleApiError(0, f"transport error: {type(exc).__name__}") from exc
            await asyncio.sleep(_backoff(attempt, delay_base))
            continue
        if resp.is_success:
            return resp
        if resp.status_code in RETRY_STATUSES and not last:
            wait = _retry_after(resp)
            if wait is None:
                wait = _backoff(attempt, delay_base)
            await asyncio.sleep(min(wait, MAX_DELAY))
            continue
        message, reason = _parse_error(resp)
        raise GoogleApiError(resp.status_code, message, reason)
    raise AssertionError("unreachable")
