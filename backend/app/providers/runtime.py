"""Resilient provider calls: cache → budget guard → rate limit → circuit breaker → retry/timeout → archive → usage.

``call()`` is the only way application code reaches an external data provider.
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infra.ratelimit import TokenBucket
from app.models import ProviderCache, ProviderUsage
from app.providers.base import BudgetExceeded, CircuitOpen, ProviderError, ProviderNotConfigured, ProviderTransientError

log = logging.getLogger(__name__)
FAILURE_THRESHOLD = 5
OPEN_SECONDS = 60


class CircuitBreaker:
    """Per-provider breaker (process-local; documented limitation: not shared across workers)."""

    def __init__(self):
        self.failures: dict[str, int] = {}
        self.opened_at: dict[str, float] = {}

    def check(self, provider: str) -> None:
        opened = self.opened_at.get(provider)
        if opened is None:
            return
        if time.monotonic() - opened < OPEN_SECONDS:
            raise CircuitOpen(f"{provider} circuit open after repeated failures; retry later")
        self.opened_at.pop(provider, None)  # half-open: allow one trial call

    def success(self, provider: str) -> None:
        self.failures[provider] = 0

    def failure(self, provider: str) -> None:
        self.failures[provider] = self.failures.get(provider, 0) + 1
        if self.failures[provider] >= FAILURE_THRESHOLD:
            self.opened_at[provider] = time.monotonic()
            log.warning("provider %s circuit opened", provider)


BREAKER = CircuitBreaker()
BUCKET = TokenBucket(prefix="provider")


def cache_key(provider: str, capability: str, params: dict) -> str:
    raw = json.dumps([provider, capability, params], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def spend(db: Session, since: datetime) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(ProviderUsage.cost_usd), 0.0))
                           .where(ProviderUsage.created_at >= since, ProviderUsage.cached.is_(False))) or 0.0)


def budget_status(db: Session) -> dict:
    s = get_settings()
    now = datetime.now(UTC)
    day = spend(db, now.replace(hour=0, minute=0, second=0, microsecond=0))
    month = spend(db, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    return {"daily_spend_usd": round(day, 4), "daily_cap_usd": s.provider_daily_budget_usd,
            "monthly_spend_usd": round(month, 4), "monthly_cap_usd": s.provider_monthly_budget_usd}


def check_budget(db: Session, estimated_cost: float) -> None:
    b = budget_status(db)
    if b["daily_spend_usd"] + estimated_cost > b["daily_cap_usd"] or b["monthly_spend_usd"] + estimated_cost > b["monthly_cap_usd"]:
        log.error("ALERT provider budget cap reached: %s (estimated %.4f)", b, estimated_cost)
        raise BudgetExceeded(f"provider budget cap reached ({b}); call refused")


def _to_json(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: _to_json(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


async def call(db: Session, provider, capability: str, params: dict, fn: Callable[[], Awaitable[Any]], *,
               org_id: int | None = None, estimated_cost_usd: float | None = None, use_cache: bool = True,
               max_retries: int = 3, base_delay: float = 0.5, archive: Callable[[bytes], str | None] | None = None) -> dict:
    """Returns {"data": canonical JSON, "cached": bool, "cost_usd": float, "provider": name, "fetched_at": iso}."""
    if not provider.is_configured():
        raise ProviderNotConfigured(f"{provider.name} not configured")
    key = cache_key(provider.name, capability, params)
    now = datetime.now(UTC)
    if use_cache:
        hit = db.get(ProviderCache, key)
        if hit is not None and hit.expires_at > now:
            from app.infra import observability as obs
            obs.PROVIDER_CALLS.inc(provider=provider.name, capability=capability, outcome="cached")
            db.add(ProviderUsage(provider=provider.name, capability=capability, org_id=org_id, cost_usd=0.0, cached=True))
            db.commit()
            return {"data": hit.response, "cached": True, "cost_usd": 0.0, "provider": provider.name,
                    "fetched_at": hit.fetched_at.isoformat(), "raw_ref": hit.raw_ref}
    cost = provider.unit_cost_usd.get(capability, 0.0) if estimated_cost_usd is None else estimated_cost_usd
    check_budget(db, cost)
    BREAKER.check(provider.name)
    await BUCKET.acquire(f"{provider.name}", rate=provider.rate_per_s, burst=max(1.0, provider.rate_per_s))
    if org_id is not None:  # per-tenant fairness: one tenant cannot use the whole provider rate
        await BUCKET.acquire(f"{provider.name}:org:{org_id}", rate=max(provider.rate_per_s / 2, 0.1), burst=1)
    start = time.monotonic()
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            result = await asyncio.wait_for(fn(), timeout=get_settings().provider_timeout_s)
            break
        except (ProviderTransientError, TimeoutError) as exc:
            last = exc
            if attempt == max_retries:
                BREAKER.failure(provider.name)
                _usage(db, provider.name, capability, org_id, 0.0, False, exc, start)
                raise ProviderTransientError(f"{provider.name} failed after retries: {exc}") from exc
            await asyncio.sleep(base_delay * 2 ** attempt * random.uniform(0.5, 1.5))
        except ProviderError as exc:
            BREAKER.failure(provider.name)
            _usage(db, provider.name, capability, org_id, 0.0, False, exc, start)
            raise
    BREAKER.success(provider.name)
    data = _to_json(result)
    raw_ref = None
    if archive is not None:
        try:
            raw_ref = archive(json.dumps(data, default=str).encode())
        except Exception as exc:  # archiving must not lose a paid response
            log.warning("raw archive failed: %s", type(exc).__name__)
    actual_cost = getattr(getattr(result, "lineage", None), "cost_usd", None) or cost
    ttl = timedelta(hours=get_settings().provider_cache_ttl_hours)
    stmt = insert(ProviderCache).values(cache_key=key, provider=provider.name, capability=capability, params=params,
                                        response=data, raw_ref=raw_ref, cost_usd=actual_cost, fetched_at=now,
                                        expires_at=now + ttl)
    db.execute(stmt.on_conflict_do_update(index_elements=["cache_key"], set_={
        "response": stmt.excluded.response, "raw_ref": stmt.excluded.raw_ref, "fetched_at": now, "expires_at": now + ttl}))
    _usage(db, provider.name, capability, org_id, actual_cost, True, None, start)
    _ = last
    return {"data": data, "cached": False, "cost_usd": actual_cost, "provider": provider.name, "fetched_at": now.isoformat(),
            "raw_ref": raw_ref}


def _usage(db: Session, provider: str, capability: str, org_id, cost: float, ok: bool, exc, start: float) -> None:
    from app.infra import observability as obs
    elapsed = time.monotonic() - start
    obs.PROVIDER_CALLS.inc(provider=provider, capability=capability, outcome="ok" if ok else "error")
    obs.PROVIDER_LATENCY.observe(elapsed, provider=provider, capability=capability)
    if cost > 0:
        obs.PROVIDER_COST.inc(cost, provider=provider)
    db.add(ProviderUsage(provider=provider, capability=capability, org_id=org_id, cost_usd=cost, cached=False, ok=ok,
                         error=(f"{type(exc).__name__}: {exc}"[:500] if exc else None),
                         latency_ms=int(elapsed * 1000)))
    db.commit()
