"""Google integrations, performance data, dashboard and opportunities."""

from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from fastapi.responses import RedirectResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, Page, date_range, get_or_404, previous_range
from app.core.config import get_settings
from app.auth import get_db
from app.models import AuditFinding, CrawlRun, GrowthOpportunity, Site
from app.schemas import OpportunityUpdate, opportunity_out
from app.services import google_service as gs
from app.services import growth_service, metrics
from app.services.integrations.retry import GoogleApiError

router = APIRouter()


def _not_configured(exc: Exception) -> ApiError:
    return ApiError(400, "integration_not_configured", str(exc), {"setup_instructions": gs.SETUP_INSTRUCTIONS})


# ---------------------------------------------------------------- Google

@router.get("/integrations/google/status")
def google_status(db: Session = Depends(get_db)):
    return gs.status(db)


@router.post("/integrations/google/connect")
def google_connect(db: Session = Depends(get_db)):
    try:
        return {"authorization_url": gs.start_authorization(db)}
    except gs.IntegrationNotConfigured as exc:
        raise _not_configured(exc) from exc


@router.get("/integrations/google/callback")
async def google_callback(state: str = Query("", max_length=256), code: str | None = Query(None, max_length=2048),
                          error: str | None = Query(None, max_length=200), db: Session = Depends(get_db)):
    target = get_settings().operator_redirect_url
    result = "connected"
    if error or not code:
        result = "error"
    else:
        try:
            await gs.complete_authorization(db, code, state)
        except (ValueError, GoogleApiError, gs.IntegrationNotConfigured):
            result = "error"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}google={result}", status_code=303)


@router.post("/integrations/google/disconnect")
def google_disconnect(db: Session = Depends(get_db)):
    gs.disconnect(db)
    return gs.status(db)


@router.get("/integrations/google/properties")
async def google_properties(db: Session = Depends(get_db)):
    try:
        return await gs.list_properties(db)
    except gs.IntegrationNotConfigured as exc:
        raise _not_configured(exc) from exc
    except GoogleApiError as exc:
        raise ApiError(502, "bad_gateway", f"Google API error: {exc}") from exc


@router.get("/sites/{site_id}/integrations/search-console")
def gsc_status(site_id: int, db: Session = Depends(get_db)):
    return gs.integration_status(db, get_or_404(db, Site, site_id, "site"), "search_console")


@router.get("/sites/{site_id}/integrations/analytics")
def ga4_status(site_id: int, db: Session = Depends(get_db)):
    return gs.integration_status(db, get_or_404(db, Site, site_id, "site"), "analytics")


def _enqueue(db: Session, site_id: int, source: str) -> dict:
    site = get_or_404(db, Site, site_id, "site")
    try:
        job = gs.enqueue_sync(db, site, source)
    except gs.IntegrationNotConfigured as exc:
        raise _not_configured(exc) from exc
    return {"job_id": job.id, "status": job.status}


@router.post("/sites/{site_id}/sync/search-console", status_code=202)
def sync_gsc(site_id: int, db: Session = Depends(get_db)):
    return _enqueue(db, site_id, "search_console")


@router.post("/sites/{site_id}/sync/analytics", status_code=202)
def sync_ga4(site_id: int, db: Session = Depends(get_db)):
    return _enqueue(db, site_id, "analytics")


# ---------------------------------------------------------------- performance

@router.get("/sites/{site_id}/search-performance")
def search_performance(site_id: int, start: date | None = None, end: date | None = None,
                       dimension: Literal["date", "page", "query", "device", "country"] = "date",
                       page: Page = Depends(), db: Session = Depends(get_db)):
    get_or_404(db, Site, site_id, "site")
    s, e = date_range(start, end)
    limit = 1000 if dimension == "date" else page.page_size
    return metrics.search_performance(db, site_id, s, e, dimension, 0 if dimension == "date" else page.offset, limit)


@router.get("/sites/{site_id}/landing-pages")
def landing_pages(site_id: int, start: date | None = None, end: date | None = None,
                  sort: Literal["sessions", "conversions", "revenue"] = "sessions", page: Page = Depends(),
                  db: Session = Depends(get_db)):
    get_or_404(db, Site, site_id, "site")
    s, e = date_range(start, end)
    return metrics.landing_pages(db, site_id, s, e, sort, page.offset, page.page_size)


@router.get("/sites/{site_id}/conversions")
def conversions(site_id: int, start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    s, e = date_range(start, end)
    return metrics.conversions(db, site_id, site.conversion_events or {}, s, e)


@router.get("/sites/{site_id}/growth")
def growth(site_id: int, start: date | None = None, end: date | None = None, compare_start: date | None = None,
           compare_end: date | None = None, db: Session = Depends(get_db)):
    get_or_404(db, Site, site_id, "site")
    s, e = date_range(start, end)
    ps, pe = (compare_start, compare_end) if compare_start and compare_end else previous_range(s, e)
    cur, prev = metrics.site_metrics(db, site_id, s, e), metrics.site_metrics(db, site_id, ps, pe)
    return {"site_id": site_id, "start": s.isoformat(), "end": e.isoformat(), "compare_start": ps.isoformat(),
            "compare_end": pe.isoformat(), "current": cur, "previous": prev, "change_pct": metrics.change_pct(cur, prev)}


@router.get("/dashboard/overview")
def overview(start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    s, e = date_range(start, end)
    ps, pe = previous_range(s, e)
    rows = []
    for site in db.scalars(select(Site).where(Site.is_active.is_(True)).order_by(Site.id)):
        last = growth_service.latest_completed_crawl(db, site.id)
        crit = db.scalar(select(func.count()).select_from(AuditFinding).where(
            AuditFinding.crawl_run_id == last.id, AuditFinding.severity == "critical")) if last else None
        last_any = db.scalar(select(func.max(CrawlRun.finished_at)).where(CrawlRun.site_id == site.id))
        rows.append({
            "site": {"id": site.id, "name": site.name, "base_url": site.base_url},
            "current": metrics.site_metrics(db, site.id, s, e), "previous": metrics.site_metrics(db, site.id, ps, pe),
            "open_opportunities": db.scalar(select(func.count()).select_from(GrowthOpportunity).where(
                GrowthOpportunity.site_id == site.id, GrowthOpportunity.status.in_(("new", "planned", "in_progress")))),
            "critical_findings": crit, "last_crawl_at": last_any.isoformat() if last_any else None,
            "gsc": gs.integration_status(db, site, "search_console"), "ga4": gs.integration_status(db, site, "analytics"),
        })
    st = gs.status(db)
    return {"start": s.isoformat(), "end": e.isoformat(), "compare_start": ps.isoformat(), "compare_end": pe.isoformat(),
            "totals": metrics.sum_metrics([r["current"] for r in rows]),
            "previous_totals": metrics.sum_metrics([r["previous"] for r in rows]),
            "sites": rows, "google": {"oauth_configured": st["oauth_configured"], "connected": st["connected"]}}


# ---------------------------------------------------------------- opportunities

_SORTS = {"priority": GrowthOpportunity.priority_score.desc(), "confidence": GrowthOpportunity.confidence.desc(),
          "created": GrowthOpportunity.created_at.desc(),
          "impact": func.coalesce(GrowthOpportunity.impact_estimate["value"].as_float(), -1).desc(),
          "effort": case({"low": 0, "medium": 1, "high": 2}, value=GrowthOpportunity.effort, else_=3)}


def _opp_query(site_id, status, type_, min_confidence, sort):
    stmt = select(GrowthOpportunity, Site.name).join(Site, Site.id == GrowthOpportunity.site_id)
    if site_id is not None:
        stmt = stmt.where(GrowthOpportunity.site_id == site_id)
    if status:
        stmt = stmt.where(GrowthOpportunity.status.in_(status.split(",")))
    if type_:
        stmt = stmt.where(GrowthOpportunity.opportunity_type.in_(type_.split(",")))
    if min_confidence is not None:
        stmt = stmt.where(GrowthOpportunity.confidence >= min_confidence)
    return stmt.order_by(_SORTS[sort], GrowthOpportunity.priority_score.desc(), GrowthOpportunity.id)


def _opp_page(db, stmt, page: Page) -> dict:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.execute(stmt.offset(page.offset).limit(page.page_size)).all()
    return {"items": [opportunity_out(o, n) for o, n in rows], "total": total, "page": page.page, "page_size": page.page_size}


Sort = Literal["priority", "confidence", "impact", "effort", "created"]


@router.get("/sites/{site_id}/opportunities")
def site_opportunities(site_id: int, status: str | None = None, type: str | None = None,
                       min_confidence: float | None = Query(None, ge=0, le=1), sort: Sort = "priority",
                       page: Page = Depends(), db: Session = Depends(get_db)):
    get_or_404(db, Site, site_id, "site")
    return _opp_page(db, _opp_query(site_id, status, type, min_confidence, sort), page)


@router.get("/opportunities")
def all_opportunities(site_id: int | None = None, status: str | None = None, type: str | None = None,
                      min_confidence: float | None = Query(None, ge=0, le=1), sort: Sort = "priority",
                      cursor: str | None = Query(None, max_length=64), page: Page = Depends(), db: Session = Depends(get_db)):
    if cursor is not None:  # keyset pagination (stable under inserts): ordered by id
        import base64
        try:
            after = int(base64.urlsafe_b64decode(cursor.encode()).decode()) if cursor else 0
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(422, "validation_error", "invalid cursor") from exc
        stmt = _opp_query(site_id, status, type, min_confidence, "priority").order_by(None).where(GrowthOpportunity.id > after) \
            .order_by(GrowthOpportunity.id).limit(page.page_size + 1)
        rows = db.execute(stmt).all()
        more = len(rows) > page.page_size
        rows = rows[:page.page_size]
        nxt = base64.urlsafe_b64encode(str(rows[-1][0].id).encode()).decode() if more and rows else None
        return {"items": [opportunity_out(o, n) for o, n in rows], "next_cursor": nxt}
    return _opp_page(db, _opp_query(site_id, status, type, min_confidence, sort), page)


@router.post("/sites/{site_id}/opportunities/generate")
def generate(site_id: int, start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    if (start is None) != (end is None):
        raise ApiError(422, "validation_error", "provide both start and end or neither")
    if start:
        date_range(start, end)
    return growth_service.generate_for_site(db, site, start, end)


@router.get("/opportunities/{opp_id}")
def get_opportunity(opp_id: int, db: Session = Depends(get_db)):
    o = get_or_404(db, GrowthOpportunity, opp_id, "opportunity")
    return opportunity_out(o, db.get(Site, o.site_id).name)


@router.patch("/opportunities/{opp_id}")
def update_opportunity(opp_id: int, body: OpportunityUpdate, db: Session = Depends(get_db)):
    o = get_or_404(db, GrowthOpportunity, opp_id, "opportunity")
    if body.status is not None and body.status != o.status:
        o.status = body.status
        if body.status == "implemented":
            o.implemented_at = datetime.now(UTC)
        elif body.status != "dismissed":
            o.implemented_at = None
    if body.notes is not None:
        o.notes = body.notes
    db.commit()
    return opportunity_out(o, db.get(Site, o.site_id).name)


@router.get("/settings/crawler")
def crawler_settings():
    s = get_settings()
    return {"user_agent": s.crawler_user_agent, "max_pages_limit": s.crawler_max_pages_limit,
            "max_depth_limit": s.crawler_max_depth_limit, "max_concurrency_limit": s.crawler_max_concurrency_limit,
            "min_request_delay_ms": s.crawler_min_request_delay_ms, "max_response_bytes": s.crawler_max_response_bytes,
            "max_redirects": s.crawler_max_redirects, "allow_private_networks": s.crawler_allow_private_networks,
            "defaults": {"max_pages": s.crawler_default_max_pages, "max_depth": s.crawler_default_max_depth,
                         "concurrency": s.crawler_default_concurrency,
                         "request_delay_ms": s.crawler_default_request_delay_ms, "timeout_s": s.crawler_default_timeout_s}}



# ---------------------------------------------------------------- Bing Webmaster (free, first-party)

class BingKeyIn(BaseModel):
    api_key: str = Field(min_length=10, max_length=200)


@router.post("/integrations/bing/connect")
def bing_connect(body: BingKeyIn, db: Session = Depends(get_db)):
    from app.auth import audit
    from app.core import crypto
    from app.models import IntegrationCredential
    p = db.info["principal"]
    cred = db.scalar(select(IntegrationCredential).where(IntegrationCredential.org_id == p.org_id,
                                                         IntegrationCredential.provider == "bing")) or \
        IntegrationCredential(org_id=p.org_id, provider="bing", scopes=["webmaster"])
    cred.encrypted_refresh_token = crypto.encrypt(body.api_key)
    db.add(cred)
    audit(db, "integration.bing_connect", None)
    db.commit()
    return {"connected": True}


@router.post("/sites/{site_id}/sync/bing")
async def bing_sync(site_id: int, db: Session = Depends(get_db)):
    from datetime import date as _date

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.core import crypto
    from app.models import BingPerformanceDaily, IntegrationCredential
    from app.providers.base import ProviderError
    from app.providers.free_sources import bing_inbound_links, bing_query_stats
    site = get_or_404(db, Site, site_id, "site")
    cred = db.scalar(select(IntegrationCredential).where(IntegrationCredential.org_id == site.org_id,
                                                         IntegrationCredential.provider == "bing"))
    if cred is None:
        raise ApiError(400, "integration_not_configured", "Bing Webmaster API key not connected (POST /integrations/bing/connect)")
    key = crypto.decrypt(cred.encrypted_refresh_token)
    try:
        rows = await bing_query_stats(key, site.base_url)
        links = await bing_inbound_links(key, site.base_url)
    except ProviderError as exc:
        raise ApiError(502, "provider_error", str(exc)) from exc
    vals = [{"site_id": site.id, "date": _date.fromisoformat(r["date"]), "query": r["query"], "clicks": r["clicks"],
             "impressions": r["impressions"], "avg_impression_position": r["avg_impression_position"],
             "avg_click_position": r["avg_click_position"]} for r in rows if r["date"]]
    if vals:
        stmt = pg_insert(BingPerformanceDaily).values(vals)
        db.execute(stmt.on_conflict_do_update(constraint="uq_bing_row", set_={c: stmt.excluded[c] for c in (
            "clicks", "impressions", "avg_impression_position", "avg_click_position")}))
        db.commit()
    indexed = 0
    from app.infra.clickhouse import ClickHouse
    ch = ClickHouse()
    if links and ch.configured:
        from app.services import backlinks
        indexed = backlinks.ingest(ch, links, "provider:bing", "FIRST_PARTY")
    return {"query_rows": len(vals), "inbound_links_indexed": indexed, "source_class": "FIRST_PARTY",
            "note": "Bing data is shown separately from Google Search Console and never summed with it."}


@router.get("/sites/{site_id}/bing-performance")
def bing_performance(site_id: int, page: Page = Depends(), db: Session = Depends(get_db)):
    from app.models import BingPerformanceDaily
    get_or_404(db, Site, site_id, "site")
    q = (select(BingPerformanceDaily.query, func.sum(BingPerformanceDaily.clicks), func.sum(BingPerformanceDaily.impressions))
         .where(BingPerformanceDaily.site_id == site_id).group_by(BingPerformanceDaily.query)
         .order_by(func.sum(BingPerformanceDaily.clicks).desc(), BingPerformanceDaily.query))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.execute(q.offset(page.offset).limit(page.page_size)).all()
    return {"source_class": "FIRST_PARTY", "engine": "bing", "has_data": total > 0, "total": total,
            "items": [{"query": qq, "clicks": int(c), "impressions": int(i)} for qq, c, i in rows]}
