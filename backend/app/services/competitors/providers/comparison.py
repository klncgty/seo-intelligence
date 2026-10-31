"""Comparison helpers for provider-supplied backlink estimates."""
from __future__ import annotations

from app.services.competitors.providers.base import BacklinkSummary


def compare_backlinks(summaries: list[BacklinkSummary]) -> list[dict]:
    """Return provider estimates ordered by known backlinks, then domain."""
    return [
        {
            "domain": summary.domain,
            "backlinks": summary.backlinks,
            "referring_domains": summary.referring_domains,
            "provider": summary.provider,
            "estimated": summary.estimated,
            "fetched_at": summary.fetched_at,
        }
        for summary in sorted(
            summaries,
            key=lambda item: (
                item.backlinks is None,
                -(item.backlinks or 0),
                item.domain,
            ),
        )
    ]
