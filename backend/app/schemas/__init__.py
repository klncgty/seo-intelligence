"""Request validation schemas + response serializers (shape documented in docs/API.md)."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.crawler.urls import host_of, normalize_url

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$|^localhost$|^[0-9.]+$")


class CrawlSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_pages: int | None = Field(None, ge=1, le=100_000)
    max_depth: int | None = Field(None, ge=0, le=50)
    concurrency: int | None = Field(None, ge=1, le=64)
    request_delay_ms: int | None = Field(None, ge=0, le=60_000)
    timeout_s: float | None = Field(None, gt=0, le=120)


class ConversionEvents(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signup: list[str] = Field(default_factory=list)
    purchase: list[str] = Field(default_factory=list)
    key_events: list[str] = Field(default_factory=list)

    @field_validator("signup", "purchase", "key_events")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", name):
                raise ValueError(f"invalid GA4 event name: {name!r}")
        return list(dict.fromkeys(v))


def _validate_base_url(v: str) -> str:
    from app.core.config import get_settings
    from app.services.crawler.ssrf import BlockedDestination, check_url

    n = normalize_url(v)
    if not n:
        raise ValueError("base_url must be an absolute http(s) URL without credentials")
    try:
        check_url(n, [host_of(n)], get_settings().crawler_allow_private_networks)
    except BlockedDestination as exc:
        raise ValueError(f"base_url not allowed: {exc}") from exc
    return n


def _validate_hosts(v: list[str]) -> list[str]:
    out = []
    for h in v:
        h = h.strip().lower().rstrip(".")
        if not _HOST_RE.match(h):
            raise ValueError(f"invalid host: {h!r}")
        out.append(h)
    return list(dict.fromkeys(out))


class SiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    base_url: str
    allowed_hosts: list[str] | None = None
    gsc_property: str | None = Field(None, max_length=512)
    ga4_property_id: str | None = Field(None, pattern=r"^(properties/)?\d{1,20}$")
    conversion_events: ConversionEvents = Field(default_factory=ConversionEvents)
    crawl_settings: CrawlSettings = Field(default_factory=CrawlSettings)
    is_active: bool = True

    _bu = field_validator("base_url")(classmethod(lambda cls, v: _validate_base_url(v)))
    _ah = field_validator("allowed_hosts")(classmethod(lambda cls, v: _validate_hosts(v) if v is not None else v))

    @model_validator(mode="after")
    def _hosts(self):
        base_host = host_of(self.base_url)
        if not self.allowed_hosts:
            self.allowed_hosts = [base_host]
        elif base_host not in self.allowed_hosts:
            self.allowed_hosts = [base_host, *self.allowed_hosts]
        return self


def _validate_money(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    import re as _re
    if len(v) > 20 or any(len(x) > 200 for x in v):
        raise ValueError("max 20 patterns of 200 chars")
    for x in v:
        try:
            _re.compile(x)
        except _re.error as exc:
            raise ValueError(f"invalid regex {x!r}: {exc}") from exc
    return v


class SiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    money_page_patterns: list[str] | None = None
    custom_extractors: list[dict] | None = None
    page_groups: list[dict] | None = None
    brand_terms: list[str] | None = Field(None, max_length=50)
    ga4_timezone: str | None = Field(None, max_length=64)
    name: str | None = Field(None, min_length=1, max_length=200)
    base_url: str | None = None
    allowed_hosts: list[str] | None = None
    gsc_property: str | None = Field(None, max_length=512)
    ga4_property_id: str | None = Field(None, pattern=r"^(properties/)?\d{1,20}$")
    conversion_events: ConversionEvents | None = None
    crawl_settings: CrawlSettings | None = None
    is_active: bool | None = None

    _bu = field_validator("base_url")(classmethod(lambda cls, v: _validate_base_url(v) if v is not None else v))
    _ah = field_validator("allowed_hosts")(classmethod(lambda cls, v: _validate_hosts(v) if v is not None else v))
    _mp = field_validator("money_page_patterns")(classmethod(lambda cls, v: _validate_money(v)))

    @field_validator("page_groups")
    @classmethod
    def _pg(cls, v):
        if v is None:
            return v
        import regex
        out = []
        for g in v[:50]:
            name, pat = str(g.get("name", ""))[:100], str(g.get("pattern", ""))
            if not name or not pat or len(pat) > 200:
                raise ValueError("page group needs name and pattern (≤200 chars)")
            try:
                regex.compile(pat)
            except regex.error as exc:
                raise ValueError(f"invalid pattern for {name!r}") from exc
            out.append({"name": name, "pattern": pat})
        return out

    @field_validator("brand_terms")
    @classmethod
    def _bt(cls, v):
        return [t.strip()[:100] for t in v if t.strip()] if v is not None else v

    @field_validator("custom_extractors")
    @classmethod
    def _ex(cls, v):
        if v is None:
            return v
        from app.services.advanced_audit import validate_extractors
        return validate_extractors(v)


class CrawlCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    crawl_settings: CrawlSettings = Field(default_factory=CrawlSettings)


class OpportunityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "planned", "in_progress", "implemented", "dismissed"] | None = None
    notes: str | None = Field(None, max_length=5000)


class DateRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self):
        if self.start > self.end:
            raise ValueError("start must be <= end")
        if (self.end - self.start).days > 731:
            raise ValueError("date range limited to 2 years")
        return self


# ------------------------------------------------------------- serializers

def iso(v):
    return v.isoformat() if v is not None else None


def crawl_out(r) -> dict:
    return {"id": r.id, "site_id": r.site_id, "competitor_id": r.competitor_id, "status": r.status, "created_at": iso(r.created_at),
            "started_at": iso(r.started_at), "finished_at": iso(r.finished_at),
            "pages_discovered": r.pages_discovered, "pages_crawled": r.pages_crawled, "pages_failed": r.pages_failed,
            "config": r.config, "error_summary": r.error_summary, "cancel_requested": r.cancel_requested,
            "sitemap_found": r.sitemap_found, "truncated": r.truncated, "health_score": r.health_score}


def site_out(s, last_crawl=None) -> dict:
    ce = s.conversion_events or {}
    return {"id": s.id, "org_id": s.org_id, "project_id": s.project_id, "name": s.name, "base_url": s.base_url, "allowed_hosts": s.allowed_hosts,
            "gsc_property": s.gsc_property, "ga4_property_id": s.ga4_property_id,
            "conversion_events": {"signup": ce.get("signup", []), "purchase": ce.get("purchase", []),
                                  "key_events": ce.get("key_events", [])},
            "crawl_settings": s.crawl_settings or {}, "is_active": s.is_active,
            "money_page_patterns": s.money_page_patterns or [], "custom_extractors": s.custom_extractors or [],
            "page_groups": s.page_groups or [], "brand_terms": s.brand_terms or [], "ga4_timezone": s.ga4_timezone,
            "verified": s.verified_at is not None, "verification_method": s.verification_method,
            "created_at": iso(s.created_at), "updated_at": iso(s.updated_at),
            "last_crawl": crawl_out(last_crawl) if last_crawl else None}


def finding_out(f) -> dict:
    return {"id": f.id, "crawl_run_id": f.crawl_run_id, "page_id": f.page_id, "url": f.url, "rule_id": f.rule_id,
            "severity": f.severity, "title": f.title, "description": f.description, "evidence": f.evidence,
            "remediation": f.remediation, "status": getattr(f, "status", "open") or "open",
            "category": getattr(f, "category", "technical") or "technical",
            "root_cause": getattr(f, "root_cause", None), "seo_impact": getattr(f, "seo_impact", None),
            "priority": getattr(f, "priority", 50) or 50}


def page_summary_out(p, findings_count: int = 0) -> dict:
    return {"id": p.id, "url": p.url, "normalized_url": p.normalized_url, "status_code": p.status_code,
            "content_type": p.content_type, "title": p.title, "word_count": p.word_count,
            "response_time_ms": p.response_time_ms, "depth": p.depth, "fetch_error": p.fetch_error,
            "findings_count": findings_count}


def opportunity_out(o, site_name: str | None = None) -> dict:
    return {"id": o.id, "site_id": o.site_id, "site_name": site_name, "url": o.url,
            "opportunity_type": o.opportunity_type, "title": o.title, "description": o.description,
            "evidence": o.evidence, "data_sources": o.data_sources, "date_range": o.date_range,
            "impact_estimate": o.impact_estimate, "confidence": o.confidence, "effort": o.effort,
            "priority_score": o.priority_score, "recommended_action": o.recommended_action,
            "validation_metric": o.validation_metric, "limitations": o.limitations, "status": o.status,
            "notes": o.notes, "implemented_at": iso(o.implemented_at),
            "resolution_suggested_at": iso(getattr(o, "resolution_suggested_at", None)),
            "reopened_count": getattr(o, "reopened_count", 0) or 0, "measurement": getattr(o, "measurement", None), "created_at": iso(o.created_at),
            "updated_at": iso(o.updated_at)}
