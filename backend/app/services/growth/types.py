"""Plain dataclasses for the growth opportunity engine (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class GscRow:
    date: date
    page: str
    query: str
    device: str
    country: str
    clicks: int
    impressions: int
    position: float


@dataclass(frozen=True)
class Ga4Row:
    date: date
    landing_page: str
    channel_group: str
    sessions: int
    total_users: int
    signups: int | None = None
    purchases: int | None = None
    revenue: float | None = None


@dataclass(frozen=True)
class PageIssue:
    url: str
    rule_id: str
    severity: str


@dataclass
class LinkGraph:
    inlinks: dict[str, int] = field(default_factory=dict)
    depth: dict[str, int] = field(default_factory=dict)


@dataclass
class GrowthInput:
    site_id: int
    base_url: str
    current_start: date
    current_end: date
    previous_start: date
    previous_end: date
    gsc_rows: list[GscRow] = field(default_factory=list)
    ga4_rows: list[Ga4Row] = field(default_factory=list)
    issues: list[PageIssue] = field(default_factory=list)
    graph: LinkGraph | None = None
    gsc_available: bool = True
    ga4_available: bool = True


@dataclass
class Opportunity:
    key: str
    opportunity_type: str
    url: str | None
    title: str
    description: str
    evidence: dict[str, Any]
    data_sources: list[str]
    date_range: dict[str, str]
    impact_estimate: dict[str, Any]
    confidence: float
    effort: str
    priority_score: float
    recommended_action: str
    validation_metric: str
    limitations: str
