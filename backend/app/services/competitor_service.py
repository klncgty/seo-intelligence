"""DB glue for Competitor Intelligence.

Data separation rule: first-party GSC data (own site only) and third-party provider estimates
(ExternalSeoSnapshot) are returned under separate keys and never summed or merged.
"""

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    AuditFinding,
    Competitor,
    CrawlPage,
    CrawlRun,
    ExternalSeoSnapshot,
    GrowthOpportunity,
    PageLink,
    PpcKeywordRecord,
    SearchPerformanceDaily,
    Site,
)
from app.services.crawler.urls import host_of

OWN_GSC_DAYS = 90


def _jsonable(obj):
    if is_dataclass(obj):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def last_crawl(db: Session, competitor_id: int, completed: bool = False) -> CrawlRun | None:
    stmt = select(CrawlRun).where(CrawlRun.competitor_id == competitor_id)
    if completed:
        stmt = stmt.where(CrawlRun.status == "completed")
    return db.scalar(stmt.order_by(CrawlRun.id.desc()).limit(1))


def _own_last_crawl(db: Session, site_id: int) -> CrawlRun | None:
    return db.scalar(select(CrawlRun).where(CrawlRun.site_id == site_id, CrawlRun.competitor_id.is_(None),
                                            CrawlRun.status == "completed").order_by(CrawlRun.id.desc()).limit(1))


def snapshot(db: Session, run: CrawlRun, label: str, base_url: str):
    from app.services.competitors.types import PageSnapshot, SiteSnapshot

    pages = db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == run.id).order_by(CrawlPage.id)).all()
    inl = dict(db.execute(
        select(PageLink.target_url, func.count(func.distinct(PageLink.source_page_id)))
        .join(CrawlPage, CrawlPage.id == PageLink.source_page_id)
        .where(CrawlPage.crawl_run_id == run.id, PageLink.is_internal.is_(True)).group_by(PageLink.target_url)).all())
    sitemap = set(run.sitemap_urls or [])
    findings = dict(db.execute(select(AuditFinding.rule_id, func.count()).where(AuditFinding.crawl_run_id == run.id)
                               .group_by(AuditFinding.rule_id)).all())
    return SiteSnapshot(
        label=label, domain=host_of(base_url).removeprefix("www."), base_url=base_url,
        pages=[PageSnapshot(url=p.normalized_url, status_code=p.status_code, is_html=p.is_html, title=p.title,
                            meta_description=p.meta_description, h1=list(p.h1 or []), canonical=p.canonical_url,
                            hreflang_count=len(p.hreflang or []), word_count=p.word_count, depth=p.depth,
                            in_sitemap=p.normalized_url in sitemap, internal_inlinks=inl.get(p.normalized_url, 0))
               for p in pages],
        sitemap_found=run.sitemap_found, sitemap_url_count=len(sitemap),
        robots_blocked_count=len(run.robots_blocked_urls or []), findings_by_rule=findings,
        crawled_at=run.finished_at.isoformat() if run.finished_at else None, crawl_id=run.id,
    )


def _snapshots(db: Session, site: Site):
    own_run = _own_last_crawl(db, site.id)
    comps = []
    for c in db.scalars(select(Competitor).where(Competitor.site_id == site.id, Competitor.is_active.is_(True))
                        .order_by(Competitor.id)):
        run = last_crawl(db, c.id, completed=True)
        if run:
            comps.append(snapshot(db, run, c.name, c.base_url))
    own = snapshot(db, own_run, "own", site.base_url) if own_run else None
    return own, comps


def own_queries(db: Session, site_id: int):
    """Own GSC queries aggregated over the last 90 days. First-party data only."""
    from app.services.competitors.types import OwnQuery

    since = date.today() - timedelta(days=OWN_GSC_DAYS)
    rows = db.execute(
        select(SearchPerformanceDaily.query, func.sum(SearchPerformanceDaily.clicks),
               func.sum(SearchPerformanceDaily.impressions),
               func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions))
        .where(SearchPerformanceDaily.site_id == site_id, SearchPerformanceDaily.date >= since)
        .group_by(SearchPerformanceDaily.query)).all()
    return [OwnQuery(query=q, clicks=int(c), impressions=int(i), position=(w / i) if i else 0.0) for q, c, i, w in rows]


def _sources(own, comps) -> list[dict]:
    out = [{"label": "own", "source": f"crawl#{own.crawl_id}", "updated_at": own.crawled_at}] if own else []
    return out + [{"label": c.label, "source": f"crawl#{c.crawl_id}", "updated_at": c.crawled_at} for c in comps]


def _require(own, comps) -> dict | None:
    if own is None:
        return {"available": False, "reason": "Own site has no completed crawl. Run a crawl first."}
    if not comps:
        return {"available": False, "reason": "No competitor has a completed crawl. Add competitors and crawl them."}
    return None


def comparison(db: Session, site: Site) -> dict:
    from app.services.competitors.comparison import compare_sites

    own, comps = _snapshots(db, site)
    if missing := _require(own, comps):
        return {**missing, "sources": _sources(own, comps)}
    return {"available": True, "sources": _sources(own, comps), **_jsonable(compare_sites(own, comps))}


def content_gaps(db: Session, site: Site, limit: int = 100) -> dict:
    from app.services.competitors.topics import content_gaps as gaps_fn

    own, comps = _snapshots(db, site)
    if missing := _require(own, comps):
        return {**missing, "sources": _sources(own, comps)}
    queries = own_queries(db, site.id)
    gaps = gaps_fn(own, comps, queries)
    return {"available": True, "sources": _sources(own, comps),
            "own_gsc": {"source": "gsc:own (first-party)", "queries_considered": len(queries),
                        "window_days": OWN_GSC_DAYS, "available": bool(queries)},
            "gaps": _jsonable(gaps[:limit]), "total": len(gaps)}


def generate_opportunities(db: Session, site: Site) -> dict:
    from app.services.competitors.comparison import compare_sites
    from app.services.competitors.opportunities import competitor_opportunities
    from app.services.competitors.topics import content_gaps as gaps_fn

    own, comps = _snapshots(db, site)
    if missing := _require(own, comps):
        return {**missing, "created": 0, "updated": 0, "total": 0}
    cmp = compare_sites(own, comps)
    gaps = gaps_fn(own, comps, own_queries(db, site.id))
    today = date.today()
    opps = competitor_opportunities(own, comps, gaps, cmp["missing_page_types"], cmp["technical_differences"],
                                    {"start": (today - timedelta(days=OWN_GSC_DAYS)).isoformat(), "end": today.isoformat()})
    existing = {o.key: o for o in db.scalars(select(GrowthOpportunity).where(GrowthOpportunity.site_id == site.id))}
    created = 0
    now = datetime.now(UTC)
    for o in opps:
        row = existing.get(o.key)
        if row is None:
            row = GrowthOpportunity(site_id=site.id, key=o.key, status="new")
            db.add(row)
            created += 1
        row.url, row.opportunity_type, row.title, row.description = o.url, o.opportunity_type, o.title[:300], o.description
        row.evidence, row.data_sources, row.date_range = _jsonable(o.evidence), o.data_sources, o.date_range
        row.impact_estimate, row.confidence, row.effort = _jsonable(o.impact_estimate), o.confidence, o.effort
        row.priority_score, row.recommended_action = o.priority_score, o.recommended_action
        row.validation_metric, row.limitations, row.last_seen_at = o.validation_metric, o.limitations, now
    db.commit()
    return {"available": True, "created": created, "updated": len(opps) - created, "total": len(opps)}


def suggest_competitors(db: Session, site: Site, limit: int, ch=None, database: str = "tr-TR:google.com.tr") -> dict:
    """Suggestions only (never auto-added): external domains the own site links to most + SERP overlap competitors."""
    own_host = host_of(site.base_url).removeprefix("www.")
    registered = set(db.scalars(select(Competitor.domain).where(Competitor.site_id == site.id)))
    serp_competitors: list[dict] = []
    if ch is not None:
        try:
            from app.services import domains as dom_service
            serp_competitors = [
                c for c in dom_service.serp_competitor_suggestions(ch, own_host, database, limit=limit)
                if c["domain"] not in registered
            ][:limit]
        except Exception:
            serp_competitors = []
    run = _own_last_crawl(db, site.id)
    if run is None:
        return {
            "suggestions": [],
            "serp_competitors": serp_competitors,
            "method": "outbound links of latest crawl + SERP keyword overlap",
            "note": "No completed crawl yet.",
        }
    counts: Counter = Counter()
    for (url,) in db.execute(select(PageLink.target_url).join(CrawlPage, CrawlPage.id == PageLink.source_page_id)
                             .where(CrawlPage.crawl_run_id == run.id, PageLink.is_internal.is_(False))):
        d = host_of(url).removeprefix("www.")
        if d and d != own_host and d not in registered:
            counts[d] += 1
    return {
        "suggestions": [{"domain": d, "outbound_links": n} for d, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]],
        "serp_competitors": serp_competitors,
        "method": "outbound links of latest own crawl + SERP keyword overlap",
        "note": "Suggestions are NOT competitors by default; many will be partners, social or tools. Add explicitly.",
    }


# ---------------------------------------------------------------- external provider

def _provider():
    from app.services.competitors.providers.registry import get_provider

    s = get_settings()
    return get_provider(s.seo_data_provider or None, s.dataforseo_login, s.dataforseo_password)


def provider_status() -> dict:
    p = _provider()
    return {"provider": p.name, "configured": p.is_configured(),
            "features": {"keyword_gap": p.is_configured(), "backlinks": p.is_configured(),
                         "referring_domains": p.is_configured()},
            "note": None if p.is_configured() else
            "No external SEO data provider configured (SEO_DATA_PROVIDER + credentials). Keyword gap and backlink "
            "features are unavailable; no estimated data is shown."}


def _unavailable(exc) -> dict:
    return {"available": False, "reason": str(exc), **provider_status()}


async def keyword_gap(db: Session, site: Site, limit: int) -> dict:
    from app.services.competitors.providers.base import ProviderNotConfigured

    comps = db.scalars(select(Competitor).where(Competitor.site_id == site.id, Competitor.is_active.is_(True))).all()
    if not comps:
        return {"available": False, "reason": "No competitors registered."}
    p = _provider()
    try:
        res = await p.keyword_gap(host_of(site.base_url).removeprefix("www."), [c.domain for c in comps], limit)
    except ProviderNotConfigured as exc:
        return _unavailable(exc)
    payload = _jsonable(res)
    db.add(ExternalSeoSnapshot(site_id=site.id, provider=p.name, kind="keyword_gap",
                               domain=host_of(site.base_url), payload=payload, estimated=True))
    db.commit()
    return {"available": True, "estimated": True, "source": f"provider:{p.name}", "data": payload}


async def backlinks(db: Session, site: Site) -> dict:
    from app.services.competitors.providers.base import ProviderNotConfigured
    from app.services.competitors.providers import compare_backlinks

    p = _provider()
    domains = [(None, host_of(site.base_url).removeprefix("www."))] + [
        (c.id, c.domain) for c in db.scalars(select(Competitor).where(Competitor.site_id == site.id,
                                                                     Competitor.is_active.is_(True)))]
    summaries = []
    try:
        for comp_id, d in domains:
            s = await p.backlink_summary(d)
            summaries.append(s)
            db.add(ExternalSeoSnapshot(site_id=site.id, competitor_id=comp_id, provider=p.name, kind="backlink_summary",
                                       domain=d, payload=_jsonable(s), estimated=True))
    except ProviderNotConfigured as exc:
        db.rollback()
        return _unavailable(exc)
    db.commit()
    return {"available": True, "estimated": True, "source": f"provider:{p.name}",
            "comparison": _jsonable(compare_backlinks(summaries))}


def external_snapshots(db: Session, site_id: int, kind: str | None) -> dict:
    stmt = select(ExternalSeoSnapshot).where(ExternalSeoSnapshot.site_id == site_id)
    if kind:
        stmt = stmt.where(ExternalSeoSnapshot.kind == kind)
    rows = db.scalars(stmt.order_by(ExternalSeoSnapshot.id.desc()).limit(50)).all()
    return {"items": [{"id": r.id, "competitor_id": r.competitor_id, "provider": r.provider, "kind": r.kind,
                       "domain": r.domain, "estimated": r.estimated, "fetched_at": r.fetched_at.isoformat(),
                       "payload": r.payload} for r in rows],
            "note": "Third-party estimates. Not comparable with first-party GSC data."}


def competitor_traffic(db: Session, ch, site: Site, database: str = "tr-TR:google.com.tr") -> dict:
    """Modül 5 Competitor Traffic Analysis with strict provenance & channel breakdown."""
    from app.services import domains as dom_service

    now_iso = datetime.now(UTC).isoformat()
    own_domain = host_of(site.base_url).removeprefix("www.")
    comps = db.scalars(select(Competitor).where(Competitor.site_id == site.id, Competitor.is_active.is_(True)).order_by(Competitor.id)).all()
    targets = [("own", own_domain, None)] + [(c.name, c.domain, c.id) for c in comps]

    items: list[dict] = []
    for label, domain, comp_id in targets:
        ov = dom_service.overview(db, ch, domain, database, site.org_id) if ch is not None else {}
        obs = ov.get("observed") or {}
        actual = ov.get("actual") or {}
        ppc_rows = db.scalars(
            select(PpcKeywordRecord).where(
                PpcKeywordRecord.site_id == site.id,
                PpcKeywordRecord.domain == domain,
            )
        ).all()
        paid_keywords_count = len(ppc_rows)
        estimated_paid_traffic = round(
            sum(
                float(r.estimated_monthly_clicks if r.estimated_monthly_clicks is not None else ((r.search_volume or 0) * 0.035))
                for r in ppc_rows
            ),
            1,
        ) if ppc_rows else 0.0
        estimated_paid_cost = round(
            sum(
                float(r.estimated_monthly_cost if r.estimated_monthly_cost is not None else ((r.search_volume or 0) * 0.035 * (r.estimated_cpc or 0)))
                for r in ppc_rows
            ),
            2,
        ) if ppc_rows else 0.0
        est_org = float((obs.get("estimated_traffic") or {}).get("value") or 0.0)
        est_val = float((obs.get("estimated_traffic_value") or {}).get("value") or 0.0)
        vis_val = float((obs.get("visibility_score") or {}).get("value") or 0.0)
        kw_count = int(obs.get("ranked_keywords") or 0)
        items.append({
            "label": label,
            "domain": domain,
            "competitor_id": comp_id,
            "is_own": comp_id is None,
            "estimated_monthly_organic_traffic": round(est_org, 1),
            "verified_organic_traffic": actual.get("gsc_clicks_30d") if comp_id is None else None,
            "organic_keywords": kw_count,
            "estimated_traffic_value_usd": round(est_val, 2),
            "visibility_score": round(vis_val, 2),
            "paid_keywords_count": paid_keywords_count,
            "estimated_paid_traffic": estimated_paid_traffic,
            "estimated_paid_cost_usd": estimated_paid_cost,
            "top_pages": (obs.get("top_pages") or [])[:10],
            "traffic_channels": {
                "organic_search": {
                    "estimated_visits": round(est_org, 1),
                    "is_estimated": True,
                    "confidence_score": 0.65 if kw_count else 0.25,
                    "confidence_level": "MEDIUM" if kw_count else "LOW",
                    "data_source": "SERP_CTR_Model",
                },
                "paid_search": {
                    "estimated_visits": estimated_paid_traffic,
                    "is_estimated": True,
                    "confidence_score": 0.55 if paid_keywords_count else 0.2,
                    "confidence_level": "MEDIUM" if paid_keywords_count else "LOW",
                    "data_source": "PPC_Intelligence_Index",
                },
                "direct": {
                    "estimated_visits": None,
                    "is_estimated": True,
                    "status": "INTEGRATION_CREDENTIALS_MISSING",
                    "note": "Direct traffic requires external clickstream panel or first-party analytics connection.",
                },
                "referral": {
                    "estimated_visits": None,
                    "is_estimated": True,
                    "status": "INTEGRATION_CREDENTIALS_MISSING",
                    "note": "Referral traffic volume requires external clickstream panel.",
                },
                "social": {
                    "estimated_visits": None,
                    "is_estimated": True,
                    "status": "INTEGRATION_CREDENTIALS_MISSING",
                    "note": "Social traffic volume requires external clickstream panel.",
                },
            },
            "provenance": {
                "dataSource": "ClickHouse_SERP_CTR_Model+PostgreSQL_PPC",
                "data_source": "ClickHouse_SERP_CTR_Model+PostgreSQL_PPC",
                "source_class": "ESTIMATED",
                "collectedAt": now_iso,
                "collected_at": now_iso,
                "updatedAt": now_iso,
                "updated_at": now_iso,
                "isEstimated": True,
                "is_estimated": True,
                "confidenceScore": 0.65 if kw_count else 0.3,
                "confidence_score": 0.65 if kw_count else 0.3,
                "confidenceLevel": "MEDIUM" if kw_count else "LOW",
                "confidence_level": "MEDIUM" if kw_count else "LOW",
            },
        })

    return {
        "site_id": site.id,
        "database": database,
        "domains": items,
        "provenance": {
            "dataSource": "ClickHouse_SERP_CTR_Model",
            "data_source": "ClickHouse_SERP_CTR_Model",
            "source_class": "ESTIMATED",
            "collectedAt": now_iso,
            "collected_at": now_iso,
            "updatedAt": now_iso,
            "updated_at": now_iso,
            "isEstimated": True,
            "is_estimated": True,
            "confidenceScore": 0.65,
            "confidence_score": 0.65,
            "confidenceLevel": "MEDIUM",
            "confidence_level": "MEDIUM",
        },
    }


def competitor_matrix(db: Session, ch, site: Site, database: str = "tr-TR:google.com.tr") -> dict:
    """Modül 5 Side-by-Side Comparison Matrix for own site + 3+ competitors + unified Keyword/Backlink/Content Gap."""
    from app.services import backlinks as bl_service
    from app.services import domains as dom_service

    now_iso = datetime.now(UTC).isoformat()
    own_domain = host_of(site.base_url).removeprefix("www.")
    comps = db.scalars(select(Competitor).where(Competitor.site_id == site.id, Competitor.is_active.is_(True)).order_by(Competitor.id)).all()
    comp_domains = [c.domain for c in comps]

    own_run = _own_last_crawl(db, site.id)
    targets = [("own", own_domain, None, own_run)] + [
        (c.name, c.domain, c.id, last_crawl(db, c.id, completed=True)) for c in comps
    ]

    matrix_rows: list[dict] = []
    for label, domain, comp_id, run in targets:
        ov = dom_service.overview(db, ch, domain, database, site.org_id) if ch is not None else {}
        obs = ov.get("observed") or {}
        bl = bl_service.overview(ch, domain) if ch is not None else {}
        word_counts = []
        if run is not None:
            word_counts = [
                w for (w,) in db.execute(
                    select(CrawlPage.word_count).where(
                        CrawlPage.crawl_run_id == run.id,
                        CrawlPage.status_code == 200,
                        CrawlPage.is_html.is_(True),
                        CrawlPage.word_count.is_not(None),
                    )
                ) if w is not None
            ]
        avg_words = round(sum(word_counts) / len(word_counts), 1) if word_counts else None
        matrix_rows.append({
            "label": label,
            "domain": domain,
            "competitor_id": comp_id,
            "is_own": comp_id is None,
            "estimated_organic_traffic": round(float((obs.get("estimated_traffic") or {}).get("value") or 0.0), 1),
            "organic_keywords": int(obs.get("ranked_keywords") or 0),
            "visibility_score": round(float((obs.get("visibility_score") or {}).get("value") or 0.0), 2),
            "total_backlinks": int(bl.get("live_links") or 0),
            "referring_domains": int(bl.get("referring_domains") or 0),
            "domain_strength": (bl.get("domain_strength") or {}).get("score"),
            "technical_health_score": run.health_score if run else None,
            "indexed_or_crawled_pages": int(run.pages_crawled or 0) if run else int((ov.get("indexed_pages") or {}).get("total_known_urls") or 0),
            "avg_word_count": avg_words,
            "crawl_id": run.id if run else None,
        })

    # Unified Keyword Gap from ClickHouse SERP index (missing, weak, strong, shared, unique, untapped)
    kw_gap_summary: dict = {"available": False, "counts": {}, "top_opportunities": []}
    if comp_domains and ch is not None:
        gap_data = dom_service.keyword_gap(ch, [own_domain, *comp_domains[:4]], database, limit=25)
        kws = gap_data.get("keywords") or {}
        kw_gap_summary = {
            "available": True,
            "counts": gap_data.get("counts") or {},
            "missing_keywords": (kws.get("missing") or [])[:15],
            "untapped_keywords": (kws.get("untapped") or [])[:15],
            "weak_keywords": (kws.get("weak") or [])[:15],
            "strong_keywords": (kws.get("strong") or [])[:10],
            "unique_keywords": (kws.get("unique") or [])[:10],
        }

    # Backlink Gap from ClickHouse backlinks intersect
    bl_gap_summary: dict = {"available": False, "prospects": [], "total_prospects": 0}
    if comp_domains and ch is not None:
        min_c = 1 if len(comp_domains) == 1 else min(2, len(comp_domains))
        inter = bl_service.intersect(ch, own_domain, comp_domains[:5], min_competitors=min_c, limit=25)
        bl_gap_summary = {
            "available": True,
            "min_competitors": min_c,
            "total_prospects": len(inter.get("prospects") or []),
            "prospects": (inter.get("prospects") or [])[:20],
        }

    # Content Gap summary from crawl snapshots
    cg = content_gaps(db, site, limit=20)
    cmp = comparison(db, site)
    content_gap_summary = {
        "available": bool(cg.get("available")),
        "total_topic_gaps": cg.get("total", 0),
        "top_topic_gaps": (cg.get("gaps") or [])[:15],
        "missing_page_types": cmp.get("missing_page_types", []) if cmp.get("available") else [],
    }

    return {
        "site_id": site.id,
        "own_domain": own_domain,
        "competitor_count": len(comps),
        "supports_multi_competitor": True,
        "recommended_min_competitors": 3,
        "matrix": matrix_rows,
        "keyword_gap_summary": kw_gap_summary,
        "backlink_gap_summary": bl_gap_summary,
        "content_gap_summary": content_gap_summary,
        "provenance": {
            "dataSource": "ClickHouse_SERP_Backlinks+PostgreSQL_Crawl",
            "data_source": "ClickHouse_SERP_Backlinks+PostgreSQL_Crawl",
            "source_class": "OBSERVED_AND_ESTIMATED",
            "collectedAt": now_iso,
            "collected_at": now_iso,
            "updatedAt": now_iso,
            "updated_at": now_iso,
            "isEstimated": True,
            "is_estimated": True,
            "confidenceScore": 0.78,
            "confidence_score": 0.78,
            "confidenceLevel": "HIGH",
            "confidence_level": "HIGH",
        },
    }
