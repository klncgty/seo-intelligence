"""Domain & competitor intelligence API (spec §7)."""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, get_or_404
from app.api.v1.keywords import _ch, _ctx, _provider_errors
from app.auth import Principal, current_principal, get_db
from app.models import Competitor, Site
from app.normalize.domains import registrable_domain
from app.normalize.locale import DEFAULT_DATABASE
from app.services import domains as dom

router = APIRouter()


def _domain(d: str) -> str:
    r = registrable_domain(d.replace("https://", "").replace("http://", "").split("/")[0])
    if not r:
        raise ApiError(422, "validation_error", "invalid domain")
    return r


@router.get("/domains/{domain}/overview")
def overview(domain: str, database: str = DEFAULT_DATABASE, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ctx = _ctx(database)
    return dom.overview(db, _ch(), _domain(domain), ctx.database, p.org_id)


@router.get("/domains/{domain}/visibility")
def visibility_detail(domain: str, database: str = DEFAULT_DATABASE, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ctx = _ctx(database)
    ov = dom.overview(db, _ch(), _domain(domain), ctx.database, p.org_id)
    return {
        "domain": ov["domain"],
        "database": ov["database"],
        "visibility": ov["visibility"],
        "observed_distribution": ov["observed"]["position_distribution"],
        "provider_distribution": ov["provider"]["position_distribution"],
        "organic_growth": ov["organic_growth"],
        "traffic_trends": ov["traffic_trends"],
    }


@router.get("/domains/{domain}/technical-analysis")
async def technical_analysis(
    domain: str,
    fetch_live: bool = Query(True),
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    return await dom.technical_analysis(db, _ch(), _domain(domain), p.org_id, fetch_live=fetch_live)


@router.get("/domains/{domain}/organic")
def organic(domain: str, database: str = DEFAULT_DATABASE, source: str = Query("observed", pattern="^(observed|provider)$"),
            limit: int = Query(200, ge=1, le=5000), _: Principal = Depends(current_principal)):
    ctx = _ctx(database)
    ch = _ch()
    d = _domain(domain)
    rows = (dom.observed_rankings if source == "observed" else dom.provider_rankings)(ch, d, ctx.database)
    return {"domain": d, "source": source, "source_class": "OBSERVED" if source == "observed" else "PROVIDER",
            "total": len(rows), "items": rows[:limit]}


class FetchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = DEFAULT_DATABASE
    limit: int = Field(500, ge=1, le=10000)
    confirm: bool = False


@router.post("/domains/{domain}/ranked-keywords/fetch")
async def fetch_ranked(domain: str, body: FetchIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ctx = _ctx(body.database)
    try:
        return await dom.fetch_ranked_keywords(db, _ch(), p.org_id, _domain(domain), ctx.database, body.limit, body.confirm,
                                               uuid.uuid4().hex)
    except Exception as exc:
        raise _provider_errors(exc) from exc


class GapIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domains: list[str] = Field(min_length=2, max_length=5)
    database: str = DEFAULT_DATABASE
    source: str = Field("observed", pattern="^(observed|provider)$")
    site_id: int | None = None  # adds the tenant's own GSC layer (FIRST_PARTY, separate)


@router.post("/domains/keyword-gap")
def keyword_gap(body: GapIn, db: Session = Depends(get_db)):
    ctx = _ctx(body.database)
    try:
        gap = dom.keyword_gap(_ch(), [_domain(d) for d in body.domains], ctx.database, body.source)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    if body.site_id:
        site = get_or_404(db, Site, body.site_id, "site")
        keys = {r["keyword_key"] for rows in gap["keywords"].values() for r in rows}
        gap["own_gsc_layer"] = dom.own_gsc_layer(db, site.id, keys, ctx.locale.language)
    return gap


@router.get("/sites/{site_id}/traffic-model")
def traffic_model(site_id: int, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    return dom.calibrate_ctr(db, site.id)


class CrossTenantCalibrateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opt_in_org_ids: list[int] = Field(default_factory=list, max_length=1000)
    min_k: int = Field(5, ge=2, le=100)


@router.post("/domains/ctr-calibration/cross-tenant")
def cross_tenant_ctr_calibration(body: CrossTenantCalibrateIn):
    from app.db.session import SessionLocal
    with SessionLocal() as sysdb:
        return dom.calibrate_ctr_cross_tenant(sysdb, body.opt_in_org_ids, body.min_k)


@router.get("/sites/{site_id}/competitor-suggestions/serp")
def serp_suggestions(site_id: int, database: str = DEFAULT_DATABASE, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    _ctx(database)
    own = registrable_domain(site.base_url.split("/")[2]) or site.base_url
    from sqlalchemy import select
    registered = set(db.scalars(select(Competitor.domain).where(Competitor.site_id == site.id)))
    sugg = [s for s in dom.serp_competitor_suggestions(_ch(), own, database) if s["domain"] not in registered]
    return {"suggestions": sugg, "method": "co-occurrence in top-10 of keywords where you rank (our SERP snapshots)",
            "note": "Suggestions only — a domain becomes a competitor only after you add it."}


@router.get("/sites/{site_id}/competitors/{comp_id}/digest")
def digest(site_id: int, comp_id: int, db: Session = Depends(get_db)):
    c = db.get(Competitor, comp_id)
    if c is None or c.site_id != site_id:
        raise ApiError(404, "not_found", "competitor not found")
    return dom.competitor_digest(db, c.id)



@router.post("/domains/keyword-gap/opportunities")
def keyword_gap_opportunities(body: GapIn, db: Session = Depends(get_db)):
    if not body.site_id:
        raise ApiError(422, "validation_error", "site_id required")
    _ctx(body.database)
    site = get_or_404(db, Site, body.site_id, "site")
    ch = _ch()
    try:
        gap = dom.keyword_gap(ch, [_domain(d) for d in body.domains], body.database, body.source)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    return dom.keyword_gap_opportunities(db, ch, site, gap)
