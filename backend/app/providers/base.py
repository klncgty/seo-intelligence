"""Capability interfaces + canonical schemas for external data.

Every canonical record carries lineage (source_class, provider, provider_request_id, fetched_at).
Providers return canonical objects only; raw payloads are archived separately by the runtime.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.normalize.locale import SearchContext

CAPABILITIES = ("serp", "keyword_metrics", "keyword_ideas", "ranked_keywords", "backlinks", "traffic",
                "ai_answer", "trends", "webmaster", "analytics", "cwv", "business_profile")


class ProviderError(Exception):
    """Permanent provider error (bad request, auth). Not retried."""


class ProviderTransientError(ProviderError):
    """Retryable (timeouts, 429, 5xx)."""


class ProviderNotConfigured(ProviderError):
    pass


class BudgetExceeded(ProviderError):
    pass


class CircuitOpen(ProviderError):
    pass


@dataclass
class Lineage:
    source_class: str  # PROVIDER | OBSERVED | MODELED | FIRST_PARTY | AI | DEMO
    provider: str
    provider_request_id: str | None = None
    fetched_at: datetime | None = None
    raw_ref: str | None = None
    cost_usd: float = 0.0
    cached: bool = False
    is_synthetic: bool = False


@dataclass
class SerpItem:
    rank_group: int
    rank_absolute: int
    result_type: str  # organic | featured_snippet | people_also_ask | local_pack | video | images | ad | ai_overview ...
    url: str | None = None
    domain: str | None = None
    title: str | None = None
    cited_domains: list[str] = field(default_factory=list)  # for ai_overview


@dataclass
class SerpResult:
    keyword: str
    context: SearchContext
    items: list[SerpItem]
    serp_features: list[str]
    related_searches: list[str]
    people_also_ask: list[str]
    lineage: Lineage


@dataclass
class KeywordMetric:
    keyword: str
    database: str
    search_volume: int | None
    volume_precision: str  # exact | range | rounded | unknown
    monthly: list[tuple[str, int | None]]  # [("2026-09", 1300), ...]
    cpc: float | None
    cpc_currency: str | None
    ads_competition: float | None
    lineage: Lineage


@dataclass
class RankedKeyword:
    keyword: str
    database: str
    position: int
    url: str
    search_volume: int | None
    lineage: Lineage


@runtime_checkable
class Provider(Protocol):
    name: str
    capabilities: tuple[str, ...]
    unit_cost_usd: dict[str, float]  # per call/keyword per capability
    rate_per_s: float
    supported_databases: tuple[str, ...]

    def is_configured(self) -> bool: ...


class SerpProvider(Provider, Protocol):
    async def fetch_serp(self, ctx: SearchContext, keyword: str, depth: int = 100) -> SerpResult: ...


class KeywordMetricsProvider(Provider, Protocol):
    async def keyword_metrics(self, ctx: SearchContext, keywords: list[str]) -> list[KeywordMetric]: ...


class KeywordIdeasProvider(Provider, Protocol):
    async def keyword_ideas(self, ctx: SearchContext, seed: str, limit: int = 100) -> list[KeywordMetric]: ...


class RankedKeywordsProvider(Provider, Protocol):
    async def ranked_keywords(self, ctx: SearchContext, domain: str, limit: int = 1000) -> list[RankedKeyword]: ...
