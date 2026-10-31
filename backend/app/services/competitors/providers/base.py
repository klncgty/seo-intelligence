"""Provider contracts and value objects for optional external SEO datasets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class KeywordGapRow:
    keyword: str
    competitor_domain: str
    competitor_position: int | None
    own_position: int | None
    search_volume: int | None
    url: str | None


@dataclass
class KeywordGapResult:
    provider: str
    rows: list[KeywordGapRow]
    estimated: bool = True
    fetched_at: str | None = None


@dataclass(frozen=True)
class BacklinkSummary:
    provider: str
    domain: str
    backlinks: int | None
    referring_domains: int | None
    estimated: bool = True
    fetched_at: str | None = None


@dataclass(frozen=True)
class ReferringDomain:
    domain: str
    backlinks: int | None
    rank: int | None


class ProviderNotConfigured(RuntimeError):
    """Raised when an external data provider cannot be used."""


class SeoDataProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    async def keyword_gap(
        self, own_domain: str, competitor_domains: list[str], limit: int = 100
    ) -> KeywordGapResult: ...

    async def backlink_summary(self, domain: str) -> BacklinkSummary: ...

    async def referring_domains(
        self, domain: str, limit: int = 100
    ) -> list[ReferringDomain]: ...
