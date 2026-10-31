"""Plain data types for the deterministic audit engine (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PageInput:
    url: str
    page_id: int | None = None
    status_code: int | None = None
    content_type: str | None = None
    is_html: bool = False
    titles: list[str] = field(default_factory=list)
    meta_descriptions: list[str] = field(default_factory=list)
    h1: list[str] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)
    meta_robots: str | None = None
    x_robots_tag: str | None = None
    hreflang: list[tuple[str, str]] = field(default_factory=list)
    word_count: int | None = None
    fetch_error: str | None = None
    redirect_chain: list[tuple[str, int]] = field(default_factory=list)
    final_url: str | None = None
    depth: int | None = None
    unminified_resources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LinkInput:
    source_url: str
    target_url: str
    is_internal: bool = True
    rel: str | None = None
    anchor_text: str | None = None


@dataclass(frozen=True)
class AuditInput:
    base_url: str
    pages: list[PageInput] = field(default_factory=list)
    links: list[LinkInput] = field(default_factory=list)
    sitemap_urls: set[str] = field(default_factory=set)
    robots_blocked_urls: set[str] = field(default_factory=set)
    sitemap_found: bool = False
    crawl_truncated: bool = False
    llms_txt_found: bool = True


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    url: str | None
    title: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
