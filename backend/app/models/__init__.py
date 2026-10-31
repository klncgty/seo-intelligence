"""SQLAlchemy models. Crawl history is preserved: deleting a site cascades, nothing else deletes crawls."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

CRAWL_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
OPPORTUNITY_STATUSES = ("new", "planned", "in_progress", "implemented", "dismissed")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(32), default="local")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    cross_tenant_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    branding: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")  # white-label {name, color, logo_url}


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # owner | admin | editor | viewer | client


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    ip_allowlist: Mapped[list] = mapped_column(JSONB, default=list)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    actor: Mapped[str] = mapped_column(String(128))  # user:<id> | apikey:<prefix> | local | system
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))  # only for auth/security events
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class CreditLedger(Base):
    """Append-only credit ledger: +grants, -charges. Balance = SUM(amount) for the org (current period)."""

    __tablename__ = "credit_ledger"
    __table_args__ = (Index("ix_credit_org_time", "org_id", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    amount: Mapped[float] = mapped_column(Float)
    operation: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    provider_cost_usd: Mapped[float | None] = mapped_column(Float)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Site(TimestampMixin, Base):
    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("org_id", "base_url", name="uq_site_org_base_url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(2048))
    allowed_hosts: Mapped[list] = mapped_column(JSONB, default=list)
    gsc_property: Mapped[str | None] = mapped_column(String(512))
    ga4_property_id: Mapped[str | None] = mapped_column(String(64))
    conversion_events: Mapped[dict] = mapped_column(JSONB, default=dict)
    crawl_settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    money_page_patterns: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")  # path regexes
    page_groups: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")  # [{name, pattern}]
    brand_terms: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    ga4_timezone: Mapped[str | None] = mapped_column(String(64))
    custom_extractors: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    verification_token: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_method: Mapped[str | None] = mapped_column(String(16))

    crawls: Mapped[list["CrawlRun"]] = relationship(back_populates="site", cascade="all, delete-orphan", passive_deletes=True)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    # Set when this run crawls a competitor of site_id instead of the site itself.
    competitor_id: Mapped[int | None] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pages_discovered: Mapped[int] = mapped_column(Integer, default=0)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_summary: Mapped[str | None] = mapped_column(Text)
    # Crawl-level facts needed by the audit engine.
    sitemap_found: Mapped[bool] = mapped_column(Boolean, default=False)
    sitemap_urls: Mapped[list] = mapped_column(JSONB, default=list)
    robots_blocked_urls: Mapped[list] = mapped_column(JSONB, default=list)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    robots_txt: Mapped[str | None] = mapped_column(Text)
    health_score: Mapped[float | None] = mapped_column(Float)

    site: Mapped[Site] = relationship(back_populates="crawls")


class CrawlPage(Base):
    __tablename__ = "crawl_pages"
    __table_args__ = (UniqueConstraint("crawl_run_id", "normalized_url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer, index=True)
    content_type: Mapped[str | None] = mapped_column(String(255))
    is_html: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str | None] = mapped_column(Text)
    titles: Mapped[list] = mapped_column(JSONB, default=list)
    meta_description: Mapped[str | None] = mapped_column(Text)
    meta_descriptions: Mapped[list] = mapped_column(JSONB, default=list)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    canonical_urls: Mapped[list] = mapped_column(JSONB, default=list)
    meta_robots: Mapped[str | None] = mapped_column(String(512))
    x_robots_tag: Mapped[str | None] = mapped_column(String(512))
    h1: Mapped[list] = mapped_column(JSONB, default=list)
    hreflang: Mapped[list] = mapped_column(JSONB, default=list)
    word_count: Mapped[int | None] = mapped_column(Integer)
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    redirect_chain: Mapped[list] = mapped_column(JSONB, default=list)
    fetch_error: Mapped[str | None] = mapped_column(Text)
    structured_data: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    template_sig: Mapped[str | None] = mapped_column(String(16), index=True)
    images_missing_alt: Mapped[int | None] = mapped_column(Integer)
    extracted: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    internal_pagerank: Mapped[float | None] = mapped_column(Float)
    warc_ref: Mapped[str | None] = mapped_column(String(255))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    links: Mapped[list["PageLink"]] = relationship(cascade="all, delete-orphan", passive_deletes=True)


class PageLink(Base):
    __tablename__ = "page_links"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_page_id: Mapped[int] = mapped_column(ForeignKey("crawl_pages.id", ondelete="CASCADE"), index=True)
    target_url: Mapped[str] = mapped_column(Text)
    anchor_text: Mapped[str | None] = mapped_column(Text)
    rel: Mapped[str | None] = mapped_column(String(255))
    is_internal: Mapped[bool] = mapped_column(Boolean)
    link_position: Mapped[str | None] = mapped_column(String(16))


class AuditFinding(Base):
    __tablename__ = "audit_findings"
    __table_args__ = (
        UniqueConstraint("crawl_run_id", "fingerprint"),
        Index("ix_findings_run_severity", "crawl_run_id", "severity"),
        Index("ix_findings_run_rule", "crawl_run_id", "rule_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    crawl_run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"))
    page_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_pages.id", ondelete="SET NULL"), index=True)
    url: Mapped[str | None] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    remediation: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")  # open | in_progress | fixed | ignored
    category: Mapped[str] = mapped_column(String(32), default="technical", server_default="technical")  # frontend | backend | infrastructure | content | technical
    root_cause: Mapped[str | None] = mapped_column(Text)
    seo_impact: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=50, server_default="50")


class SearchPerformanceDaily(Base):
    """One row per GSC (date, page, query, device, country). Never aggregated across dimensions on write."""

    __tablename__ = "search_performance_daily"
    __table_args__ = (
        UniqueConstraint("site_id", "date", "page", "query", "device", "country", name="uq_gsc_row"),
        Index("ix_gsc_site_date", "site_id", "date"),
        Index("ix_gsc_site_page", "site_id", "page"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    page: Mapped[str] = mapped_column(Text)
    query: Mapped[str] = mapped_column(Text)
    device: Mapped[str] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(8))
    clicks: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    ctr: Mapped[float] = mapped_column(Float)
    position: Mapped[float] = mapped_column(Float)


class OrganicLandingDaily(Base):
    __tablename__ = "organic_landing_daily"
    __table_args__ = (
        UniqueConstraint("site_id", "date", "landing_page", "channel_group", "source_medium", name="uq_ga4_row"),
        Index("ix_ga4_site_date", "site_id", "date"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    landing_page: Mapped[str] = mapped_column(Text)
    channel_group: Mapped[str] = mapped_column(String(64))
    source_medium: Mapped[str] = mapped_column(String(255))
    sessions: Mapped[int] = mapped_column(Integer)
    total_users: Mapped[int] = mapped_column(Integer)
    active_users: Mapped[int | None] = mapped_column(Integer)
    key_events: Mapped[float | None] = mapped_column(Float)
    signups: Mapped[int | None] = mapped_column(Integer)
    purchases: Mapped[int | None] = mapped_column(Integer)
    revenue: Mapped[float | None] = mapped_column(Numeric(14, 2))


class GrowthOpportunity(TimestampMixin, Base):
    __tablename__ = "growth_opportunities"
    __table_args__ = (UniqueConstraint("site_id", "key"), Index("ix_opp_site_status", "site_id", "status"))
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(Text)
    opportunity_type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    data_sources: Mapped[list] = mapped_column(JSONB, default=list)
    date_range: Mapped[dict | None] = mapped_column(JSONB)
    impact_estimate: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    effort: Mapped[str] = mapped_column(String(16))
    priority_score: Mapped[float] = mapped_column(Float, index=True)
    recommended_action: Mapped[str] = mapped_column(Text)
    validation_metric: Mapped[str] = mapped_column(Text)
    limitations: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="new")
    notes: Mapped[str | None] = mapped_column(Text)
    implemented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_suggested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    measurement: Mapped[dict | None] = mapped_column(JSONB)


class IntegrationCredential(TimestampMixin, Base):
    """Encrypted Google refresh token. Single local operator → one row per provider."""

    __tablename__ = "integration_credentials"
    __table_args__ = (UniqueConstraint("org_id", "provider", name="uq_cred_org_provider"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list] = mapped_column(JSONB, default=list)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    code_verifier_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class SyncJob(Base):
    """Background sync job (search_console | analytics). Claimed by workers with SKIP LOCKED."""

    __tablename__ = "sync_jobs"
    __table_args__ = (Index("ix_sync_site_source", "site_id", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    rows_synced: Mapped[int] = mapped_column(Integer, default=0)
    data_start: Mapped[date | None] = mapped_column(Date)
    data_end: Mapped[date | None] = mapped_column(Date)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)  # GA4 sampling/thresholding/(other)/quota; GSC preliminary range


class SearchTotalsDaily(Base):
    """GSC site totals per (date, device) fetched WITHOUT page/query dimensions.

    Query-level rows omit anonymized queries, so summing them under-reports clicks/impressions.
    Site-level totals and date charts must come from this table instead.
    """

    __tablename__ = "search_totals_daily"
    __table_args__ = (UniqueConstraint("site_id", "date", "device", name="uq_gsc_totals"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    device: Mapped[str] = mapped_column(String(16))
    clicks: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    ctr: Mapped[float] = mapped_column(Float)
    position: Mapped[float] = mapped_column(Float)


class Competitor(TimestampMixin, Base):
    """A competitor explicitly registered by the operator for one of their sites."""

    __tablename__ = "competitors"
    __table_args__ = (UniqueConstraint("site_id", "domain"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(253))
    base_url: Mapped[str] = mapped_column(String(2048))
    allowed_hosts: Mapped[list] = mapped_column(JSONB, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    crawl_settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ExternalSeoSnapshot(Base):
    """Provider-estimated data (keyword gap, backlinks). Kept separate from first-party GSC tables."""

    __tablename__ = "external_seo_snapshots"
    __table_args__ = (Index("ix_ext_site_kind", "site_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    competitor_id: Mapped[int | None] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))  # keyword_gap | backlink_summary | referring_domains
    domain: Mapped[str] = mapped_column(String(253))
    payload: Mapped[dict] = mapped_column(JSONB)
    estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    """Generic background job (ADR 0004). Claimed with SKIP LOCKED + lease; DAG via JobDependency."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_after", "priority"),
        Index("ix_jobs_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # waiting (deps pending) | queued | running | succeeded | failed (will retry) | dead | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = sooner
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    checkpoint: Mapped[dict | None] = mapped_column(JSONB)
    progress: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobDependency(Base):
    __tablename__ = "job_dependencies"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    depends_on_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True, index=True)


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    interval_s: Mapped[int] = mapped_column(Integer)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_job_id: Mapped[int | None] = mapped_column(BigInteger)


class ProviderCache(Base):
    """Global response cache: same provider+capability+params within TTL is never paid twice (shared by tenants)."""

    __tablename__ = "provider_cache"
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    capability: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSONB)
    response: Mapped[dict] = mapped_column(JSONB)  # canonical (normalized) payload
    raw_ref: Mapped[str | None] = mapped_column(String(255))  # MinIO key of the raw provider response
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProviderUsage(Base):
    """Per-call provider spend for budget caps and admin margin reporting."""

    __tablename__ = "provider_usage"
    __table_args__ = (Index("ix_provider_usage_time", "provider", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(String(64))
    org_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    cost_usd: Mapped[float] = mapped_column(Float)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(500))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KeywordList(TimestampMixin, Base):
    __tablename__ = "keyword_lists"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    database: Mapped[str] = mapped_column(String(64))


class KeywordListItem(Base):
    __tablename__ = "keyword_list_items"
    __table_args__ = (UniqueConstraint("list_id", "keyword_key"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("keyword_lists.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    keyword_key: Mapped[str] = mapped_column(String(500))
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    target_url: Mapped[str | None] = mapped_column(Text)


class RankProject(TimestampMixin, Base):
    __tablename__ = "rank_projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    own_domain: Mapped[str] = mapped_column(String(253))
    competitor_domains: Mapped[list] = mapped_column(JSONB, default=list)
    database: Mapped[str] = mapped_column(String(64))
    devices: Mapped[list] = mapped_column(JSONB, default=list)
    locations: Mapped[list] = mapped_column(JSONB, default=list)  # "" = country-level
    frequency: Mapped[str] = mapped_column(String(16), default="daily")
    depth: Mapped[int] = mapped_column(Integer, default=100)
    budget_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)  # operator accepted the recurring credit estimate
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_check_date: Mapped[date | None] = mapped_column(Date)


class TrackedKeyword(Base):
    __tablename__ = "tracked_keywords"
    __table_args__ = (UniqueConstraint("rank_project_id", "keyword_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    rank_project_id: Mapped[int] = mapped_column(ForeignKey("rank_projects.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    keyword_key: Mapped[str] = mapped_column(String(500))
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    target_url: Mapped[str | None] = mapped_column(Text)
    important: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RankAlert(Base):
    __tablename__ = "rank_alerts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    rank_project_id: Mapped[int] = mapped_column(ForeignKey("rank_projects.id", ondelete="CASCADE"), index=True)
    alert_type: Mapped[str] = mapped_column(String(32))
    keyword: Mapped[str] = mapped_column(String(500))
    severity: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BotHitDaily(Base):
    """Aggregated search-bot hits from uploaded server logs. Non-bot visitor IPs are never stored."""

    __tablename__ = "bot_hits_daily"
    __table_args__ = (UniqueConstraint("site_id", "date", "bot", "verified", "path", "status", name="uq_bot_hit"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    bot: Mapped[str] = mapped_column(String(32))
    verified: Mapped[bool] = mapped_column(Boolean)
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[int] = mapped_column(Integer)
    hits: Mapped[int] = mapped_column(Integer)


class BingPerformanceDaily(Base):
    """Bing Webmaster query stats (FIRST_PARTY, separate from Google GSC data — never summed with it)."""

    __tablename__ = "bing_performance_daily"
    __table_args__ = (UniqueConstraint("site_id", "date", "query", name="uq_bing_row"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    query: Mapped[str] = mapped_column(Text)
    clicks: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    avg_impression_position: Mapped[float | None] = mapped_column(Float)
    avg_click_position: Mapped[float | None] = mapped_column(Float)


class CalendarEvent(Base):
    """Holidays, campaigns, algorithm-update annotations. org_id NULL = global (e.g. TR public holidays)."""

    __tablename__ = "calendar_events"
    __table_args__ = (Index("ix_calendar_range", "start_date", "end_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # holiday | campaign | annotation | search_update
    name: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(64), default="user")


class AiPromptSet(TimestampMixin, Base):
    __tablename__ = "ai_prompt_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    database: Mapped[str] = mapped_column(String(64))
    own_domain: Mapped[str] = mapped_column(String(253))
    brand_terms: Mapped[list] = mapped_column(JSONB, default=list)
    competitors: Mapped[list] = mapped_column(JSONB, default=list)  # [{name, domain, terms:[...]}]
    engines: Mapped[list] = mapped_column(JSONB, default=list)
    samples_per_prompt: Mapped[int] = mapped_column(Integer, default=3)


class AiPrompt(Base):
    __tablename__ = "ai_prompts"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    set_id: Mapped[int] = mapped_column(ForeignKey("ai_prompt_sets.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), default="category")  # brand | category | comparison | purchase


class NotificationChannel(TimestampMixin, Base):
    __tablename__ = "notification_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # webhook | slack | teams | email
    name: Mapped[str] = mapped_column(String(200))
    config_encrypted: Mapped[str] = mapped_column(Text)  # JSON {url, secret} / {to}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AlertRule(TimestampMixin, Base):
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    rule_type: Mapped[str] = mapped_column(String(32))  # metric_change | anomaly | rank_alerts | crawl_regression
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    min_volume: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=24)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel_ids: Mapped[list] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    severity: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)
    deliveries: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShareLink(Base):
    __tablename__ = "share_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    resource: Mapped[str] = mapped_column(String(64))  # report:site:<id>
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)  # org:<id>:<method>:<path>:<client key>
    status_code: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class TicketLink(Base):
    __tablename__ = "ticket_links"
    __table_args__ = (UniqueConstraint("opportunity_id", "tracker"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("growth_opportunities.id", ondelete="CASCADE"))
    tracker: Mapped[str] = mapped_column(String(16))  # github | jira | linear
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="open")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SplitTest(TimestampMixin, Base):
    """SEO split test: pages of one template split into control/variant groups (stratified by clicks)."""

    __tablename__ = "split_tests"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    hypothesis: Mapped[str] = mapped_column(Text)
    template_sig: Mapped[str | None] = mapped_column(String(16))
    control_urls: Mapped[list] = mapped_column(JSONB, default=list)
    variant_urls: Mapped[list] = mapped_column(JSONB, default=list)
    started_at: Mapped[date | None] = mapped_column(Date)
    result: Mapped[dict | None] = mapped_column(JSONB)


class DomainAnalysisRecord(TimestampMixin, Base):
    """Persisted Domain Analysis & Technical SEO snapshot per organization and domain (Module 1)."""

    __tablename__ = "domain_analysis_records"
    __table_args__ = (Index("ix_dom_analysis_org_domain", "org_id", "domain"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    domain: Mapped[str] = mapped_column(String(253), index=True)
    database: Mapped[str] = mapped_column(String(64))
    overview_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    technical_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    data_source: Mapped[str] = mapped_column(String(64), default="Internal_Crawler")
    source_class: Mapped[str] = mapped_column(String(32), default="OBSERVED")
    is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.8)
    confidence_level: Mapped[str] = mapped_column(String(16), default="HIGH")


class ContentTemplateRecord(TimestampMixin, Base):
    """Persisted SEO Content Template & On-Page Audit record (Module 7)."""

    __tablename__ = "content_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    keyword: Mapped[str] = mapped_column(String(300), index=True)
    database: Mapped[str] = mapped_column(String(64))
    target_url: Mapped[str | None] = mapped_column(Text)
    template_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    onpage_check: Mapped[dict | None] = mapped_column(JSONB)
    data_source: Mapped[str] = mapped_column(String(64), default="SERP_Analyzer")
    source_class: Mapped[str] = mapped_column(String(32), default="OBSERVED")
    confidence_score: Mapped[float] = mapped_column(Float, default=0.75)


class LinkProspect(TimestampMixin, Base):
    """Link building prospect & outreach CRM record (Module 8)."""

    __tablename__ = "link_prospects"
    __table_args__ = (UniqueConstraint("org_id", "source_url", "target_url", name="uq_prospect_org_urls"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(253), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    target_url: Mapped[str | None] = mapped_column(Text)
    link_type: Mapped[str] = mapped_column(String(32))  # competitor_link | link_gap | broken_link | unlinked_mention | resource_page | guest_post | relevant_site
    relevance_score: Mapped[float] = mapped_column(Float, default=0.5)
    domain_strength: Mapped[float | None] = mapped_column(Float)
    opportunity_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    contact_info: Mapped[dict] = mapped_column(JSONB, default=dict)  # public verified contact only
    outreach_status: Mapped[str] = mapped_column(String(32), default="PROSPECT", index=True)  # PROSPECT | CONTACTED | REPLIED | NEGOTIATING | LINK_ACQUIRED | REJECTED
    email_draft: Mapped[dict | None] = mapped_column(JSONB)
    outreach_history: Mapped[list] = mapped_column(JSONB, default=list)
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_source: Mapped[str] = mapped_column(String(64), default="Internal_Crawler")
    source_class: Mapped[str] = mapped_column(String(32), default="OBSERVED")
    confidence_score: Mapped[float] = mapped_column(Float, default=0.7)


class PpcKeywordRecord(TimestampMixin, Base):
    """Competitor Paid Keyword & Ad Copy snapshot (Module 9)."""

    __tablename__ = "ppc_keywords"
    __table_args__ = (Index("ix_ppc_org_domain", "org_id", "domain"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    domain: Mapped[str] = mapped_column(String(253), index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    keyword_key: Mapped[str] = mapped_column(String(500), index=True)
    database: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer, default=1)
    search_volume: Mapped[int | None] = mapped_column(Integer)
    estimated_cpc: Mapped[float | None] = mapped_column(Float)
    competition: Mapped[float | None] = mapped_column(Float)
    ad_headline: Mapped[str | None] = mapped_column(String(500))
    ad_description: Mapped[str | None] = mapped_column(Text)
    landing_page: Mapped[str | None] = mapped_column(Text)
    estimated_monthly_clicks: Mapped[float | None] = mapped_column(Float)
    estimated_monthly_cost: Mapped[float | None] = mapped_column(Float)
    is_estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    data_source: Mapped[str] = mapped_column(String(64), default="SERP_Analyzer")
    source_class: Mapped[str] = mapped_column(String(32), default="OBSERVED")
    confidence_score: Mapped[float] = mapped_column(Float, default=0.65)


class SocialAccount(TimestampMixin, Base):
    """Connected social media account (Module 10)."""

    __tablename__ = "social_accounts"
    __table_args__ = (UniqueConstraint("org_id", "platform", "handle", name="uq_social_org_platform_handle"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(32))  # instagram | facebook | linkedin | x
    handle: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(32), default="credentials_missing")  # connected | credentials_missing | expired
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SocialPost(TimestampMixin, Base):
    """Social media post draft, scheduled item, or published record (Module 10)."""

    __tablename__ = "social_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("social_accounts.id", ondelete="SET NULL"))
    platforms: Mapped[list] = mapped_column(JSONB, default=list)
    title: Mapped[str | None] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    hashtags: Mapped[list] = mapped_column(JSONB, default=list)
    media_urls: Mapped[list] = mapped_column(JSONB, default=list)
    source_page_url: Mapped[str | None] = mapped_column(Text)
    target_keyword: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)  # draft | pending_approval | approved | scheduled | published | failed
    user_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_post_url: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)


class SeoDiagnosisRecord(TimestampMixin, Base):
    """Cross-module AI SEO Diagnosis record (Module 11)."""

    __tablename__ = "seo_diagnoses"
    __table_args__ = (UniqueConstraint("site_id", "fingerprint", name="uq_diagnosis_site_fp"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    problem: Mapped[str] = mapped_column(String(400))
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    affected_url: Mapped[str | None] = mapped_column(Text)
    source_module: Mapped[str] = mapped_column(String(64))
    data_source: Mapped[str] = mapped_column(String(64))
    source_class: Mapped[str] = mapped_column(String(32))
    root_cause: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))  # Frontend | Backend | Infrastructure | Content
    severity: Mapped[str] = mapped_column(String(16))  # CRITICAL | HIGH | MEDIUM | LOW
    seo_impact: Mapped[str] = mapped_column(Text)
    recommended_fix: Mapped[str] = mapped_column(Text)
    implementation_steps: Mapped[list] = mapped_column(JSONB, default=list)
    estimated_effort: Mapped[str] = mapped_column(String(16))  # Low | Medium | High
    confidence_score: Mapped[float] = mapped_column(Float, default=0.8)
    verification_method: Mapped[str] = mapped_column(Text)
    affected_file: Mapped[str | None] = mapped_column(Text)
    priority_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)  # new | planned | in_progress | completed | ignored


class CodeRepository(TimestampMixin, Base):
    """Connected source code repository for Deep Code Analysis & Fix Agent (Module 14)."""

    __tablename__ = "code_repositories"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    repo_type: Mapped[str] = mapped_column(String(16), default="local")  # local | github
    local_path: Mapped[str | None] = mapped_column(Text)
    github_repo: Mapped[str | None] = mapped_column(String(300))
    default_branch: Mapped[str] = mapped_column(String(64), default="main")
    detected_framework: Mapped[str | None] = mapped_column(String(64))
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_summary: Mapped[dict] = mapped_column(JSONB, default=dict)


class CodeFixProposal(TimestampMixin, Base):
    """AST/Source Code SEO Fix Proposal with diff & approval gate (Module 14)."""

    __tablename__ = "code_fix_proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"), index=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("code_repositories.id", ondelete="CASCADE"), index=True)
    diagnosis_id: Mapped[int | None] = mapped_column(ForeignKey("seo_diagnoses.id", ondelete="SET NULL"))
    finding_rule_id: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(32))  # frontend | backend
    severity: Mapped[str] = mapped_column(String(16))  # CRITICAL | HIGH | MEDIUM | LOW
    file_path: Mapped[str] = mapped_column(Text)
    line_start: Mapped[int] = mapped_column(Integer, default=1)
    line_end: Mapped[int] = mapped_column(Integer, default=1)
    issue_description: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    seo_impact: Mapped[str] = mapped_column(Text)
    original_snippet: Mapped[str] = mapped_column(Text)
    proposed_snippet: Mapped[str] = mapped_column(Text)
    unified_diff: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)  # proposed | approved_applied | verified | rejected | reverted
    user_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_report: Mapped[dict] = mapped_column(JSONB, default=dict)
