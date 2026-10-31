"""Plain dataclasses for competitor analysis (no database or network I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PageSnapshot:
    url: str
    status_code: int | None
    is_html: bool
    title: str | None
    meta_description: str | None
    h1: list[str] = field(default_factory=list)
    canonical: str | None = None
    hreflang_count: int = 0
    word_count: int | None = None
    depth: int = 0
    in_sitemap: bool = False
    internal_inlinks: int = 0


@dataclass
class SiteSnapshot:
    label: str
    domain: str
    base_url: str
    pages: list[PageSnapshot] = field(default_factory=list)
    sitemap_found: bool = False
    sitemap_url_count: int = 0
    robots_blocked_count: int = 0
    findings_by_rule: dict[str, int] = field(default_factory=dict)
    crawled_at: str | None = None
    crawl_id: int | None = None


@dataclass(frozen=True)
class OwnQuery:
    query: str
    clicks: int
    impressions: int
    position: float
