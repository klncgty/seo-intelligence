"""Loads crawl/GSC/GA4 evidence from the DB, runs the deterministic growth engine, upserts results."""

from datetime import UTC, date, datetime, timedelta
from urllib.parse import urljoin

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditFinding,
    CrawlPage,
    CrawlRun,
    GrowthOpportunity,
    OrganicLandingDaily,
    PageLink,
    SearchPerformanceDaily,
    Site,
)
from app.services.growth.engine import INDEXING_RULES, _opp, _prioritize, generate_opportunities
from app.services.growth.types import Ga4Row, GrowthInput, GscRow, LinkGraph, Opportunity, PageIssue

ORGANIC_CHANNEL = "Organic Search"


def default_periods(end: date | None = None, days: int = 28) -> tuple[date, date, date, date]:
    end = end or date.today() - timedelta(days=3)  # GSC data lags ~2-3 days
    start = end - timedelta(days=days - 1)
    return start, end, start - timedelta(days=days), start - timedelta(days=1)


def latest_completed_crawl(db: Session, site_id: int) -> CrawlRun | None:
    return db.scalar(select(CrawlRun).where(CrawlRun.site_id == site_id, CrawlRun.competitor_id.is_(None),
                                            CrawlRun.status == "completed")
                     .order_by(CrawlRun.id.desc()).limit(1))


def build_input(db: Session, site: Site, start: date, end: date, pstart: date, pend: date) -> GrowthInput:
    load_from = min(pstart, start - timedelta(days=366))
    gsc = db.execute(select(SearchPerformanceDaily).where(
        SearchPerformanceDaily.site_id == site.id, SearchPerformanceDaily.date.between(load_from, end))).scalars()
    gsc_rows = [GscRow(r.date, r.page, r.query, r.device, r.country, r.clicks, r.impressions, r.position) for r in gsc]
    ga4 = db.execute(select(OrganicLandingDaily).where(
        OrganicLandingDaily.site_id == site.id, OrganicLandingDaily.channel_group == ORGANIC_CHANNEL,
        OrganicLandingDaily.date.between(pstart, end))).scalars()
    ga4_rows = [Ga4Row(r.date, r.landing_page, r.channel_group, r.sessions, r.total_users, r.signups, r.purchases,
                       float(r.revenue) if r.revenue is not None else None) for r in ga4]
    issues: list[PageIssue] = []
    graph = None
    crawl = latest_completed_crawl(db, site.id)
    if crawl:
        issues = [PageIssue(u, rid, sev) for u, rid, sev in db.execute(
            select(AuditFinding.url, AuditFinding.rule_id, AuditFinding.severity)
            .where(AuditFinding.crawl_run_id == crawl.id, AuditFinding.url.is_not(None)))]
        depth = dict(db.execute(select(CrawlPage.normalized_url, CrawlPage.depth)
                                .where(CrawlPage.crawl_run_id == crawl.id)).all())
        inl = dict(db.execute(
            select(PageLink.target_url, func.count(func.distinct(PageLink.source_page_id)))
            .join(CrawlPage, CrawlPage.id == PageLink.source_page_id)
            .where(CrawlPage.crawl_run_id == crawl.id, PageLink.is_internal.is_(True))
            .group_by(PageLink.target_url)).all())
        graph = LinkGraph(inlinks={u: inl.get(u, 0) for u in depth}, depth=depth)
    return GrowthInput(site_id=site.id, base_url=site.base_url, current_start=start, current_end=end,
                       previous_start=pstart, previous_end=pend, gsc_rows=gsc_rows, ga4_rows=ga4_rows,
                       issues=issues, graph=graph, gsc_available=bool(gsc_rows), ga4_available=bool(ga4_rows))


def _crawl_only_opportunities(db: Session, inp: GrowthInput, crawl: CrawlRun) -> list[Opportunity]:
    """When GSC/GA4 are not yet connected, derive actionable visibility & technical SEO opportunities from crawl findings."""
    findings = db.scalars(
        select(AuditFinding).where(AuditFinding.crawl_run_id == crawl.id).order_by(AuditFinding.id)
    ).all()
    by_rule: dict[str, list[AuditFinding]] = {}
    for f in findings:
        if f.severity == "info":
            continue
        by_rule.setdefault(f.rule_id, []).append(f)

    sev_conf = {"critical": 0.6, "high": 0.55, "medium": 0.5, "low": 0.4}
    out: list[Opportunity] = []
    for rule_id, rows in sorted(by_rule.items()):
        first = rows[0]
        urls = [r.url for r in rows if r.url]
        if rule_id in INDEXING_RULES:
            opp_type = "indexing_issue_on_valuable_page"
            effort = "low"
        elif "link" in rule_id or "orphan" in rule_id or "depth" in rule_id:
            opp_type = "weak_internal_linking"
            effort = "low"
        elif "title" in rule_id or "meta_description" in rule_id:
            opp_type = "low_ctr"
            effort = "low"
        else:
            opp_type = "content_intent"
            effort = "medium"

        out.append(
            _opp(
                inp,
                key=f"crawl_audit|{rule_id}",
                type_=opp_type,
                url=urls[0] if urls else inp.base_url,
                title=f"{first.title} ({len(rows)} sayfa)",
                description=f"{first.description} — Arama motoru görünürlüğünü ve sayfa kalitesini doğrudan etkiler.",
                evidence={
                    "rule_id": rule_id,
                    "severity": first.severity,
                    "affected_pages_count": len(rows),
                    "affected_urls": urls[:15],
                },
                sources=["crawl"],
                metric="affected_pages",
                value=float(len(rows)),
                unit="pages",
                method="Tarama (crawl) sırasında tespit edilen etkilenen sayfa sayısı (OBSERVED).",
                confidence=sev_conf.get(first.severity, 0.45),
                effort=effort,
                action=first.remediation or "Etkilenen sayfalardaki teknik/içerik SEO bulgusunu düzeltin ve yeniden tarayın.",
                validation="Sonraki site taramasında (crawl) bu kurala ait bulgu sayısının sıfırlanması",
                limitations="Gözlenen (OBSERVED) site taramasına dayanır; Search Console bağlandığında tıklama/gösterim etkisiyle zenginleşir.",
            )
        )
    return _prioritize(out)


def generate_for_site(db: Session, site: Site, start: date | None = None, end: date | None = None) -> dict:
    if start and end:
        days = (end - start).days + 1
        pend = start - timedelta(days=1)
        periods = (start, end, pend - timedelta(days=days - 1), pend)
    else:
        periods = default_periods()
    inp = build_input(db, site, *periods)
    opps = generate_opportunities(inp)
    if not opps and not inp.gsc_available and not inp.ga4_available:
        crawl = latest_completed_crawl(db, site.id)
        if crawl is not None:
            opps = _crawl_only_opportunities(db, inp, crawl)
    now = datetime.now(UTC)
    existing = {o.key: o for o in db.scalars(select(GrowthOpportunity).where(GrowthOpportunity.site_id == site.id))}
    created = updated = 0
    for o in opps:
        row = existing.get(o.key)
        if row is None:
            row = GrowthOpportunity(site_id=site.id, key=o.key, status="new")
            db.add(row)
            created += 1
        else:
            updated += 1  # evidence refreshed; operator-set status/notes/implemented_at are preserved
        url = urljoin(site.base_url, o.url) if o.url and o.url.startswith("/") else o.url
        row.url, row.opportunity_type, row.title, row.description = url, o.opportunity_type, o.title[:300], o.description
        row.evidence, row.data_sources, row.date_range = o.evidence, o.data_sources, o.date_range
        row.impact_estimate, row.confidence, row.effort = o.impact_estimate, o.confidence, o.effort
        row.priority_score, row.recommended_action = o.priority_score, o.recommended_action
        row.validation_metric, row.limitations, row.last_seen_at = o.validation_metric, o.limitations, now
    db.commit()
    return {"created": created, "updated": updated, "total": len(opps),
            "period": {"start": periods[0].isoformat(), "end": periods[1].isoformat(),
                       "compare_start": periods[2].isoformat(), "compare_end": periods[3].isoformat()}}
