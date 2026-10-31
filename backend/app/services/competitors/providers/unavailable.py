"""Explicit unavailable-provider behavior; never returns synthetic metrics."""
from __future__ import annotations

from app.services.competitors.providers.base import (
    BacklinkSummary,
    KeywordGapResult,
    ProviderNotConfigured,
    ReferringDomain,
)


class UnavailableProvider:
    name = "unavailable"

    def __init__(self, hint: str = "Configure DataForSEO login and password.") -> None:
        self.hint = hint

    def is_configured(self) -> bool:
        return False

    def _error(self) -> ProviderNotConfigured:
        return ProviderNotConfigured(
            f"SEO data provider is unavailable. {self.hint}"
        )

    async def keyword_gap(
        self, own_domain: str, competitor_domains: list[str], limit: int = 100
    ) -> KeywordGapResult:
        raise self._error()

    async def backlink_summary(self, domain: str) -> BacklinkSummary:
        raise self._error()

    async def referring_domains(self, domain: str, limit: int = 100) -> list[ReferringDomain]:
        raise self._error()
