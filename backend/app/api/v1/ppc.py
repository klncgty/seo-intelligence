"""Module 9: PPC Intelligence API."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.v1.common import get_or_404
from app.auth import Principal, current_principal, get_db
from app.models import PpcKeywordRecord, Site
from app.normalize.domains import registrable_domain
from app.normalize.locale import DEFAULT_DATABASE
from app.normalize.text import match_key
from app.services import ppc
from app.services.crawler.urls import host_of

router = APIRouter()


def _optional_ch():
    try:
        from app.api.v1.keywords import _ch
        return _ch()
    except Exception:
        return None


class PpcKeywordCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(min_length=3, max_length=253)
    keyword: str = Field(min_length=1, max_length=500)
    database: str = DEFAULT_DATABASE
    position: int = Field(1, ge=1, le=20)
    search_volume: int | None = Field(None, ge=0)
    estimated_cpc: float | None = Field(None, ge=0.0)
    competition: float | None = Field(None, ge=0.0, le=1.0)
    ad_headline: str | None = Field(None, max_length=500)
    ad_description: str | None = Field(None, max_length=2000)
    landing_page: str | None = Field(None, max_length=2048)


@router.get("/sites/{site_id}/ppc/overview")
def get_ppc_overview(
    site_id: int,
    database: str = DEFAULT_DATABASE,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
):
    from app.api.v1.keywords import _ctx
    site = get_or_404(db, Site, site_id, "site")
    return ppc.ppc_overview(db, _optional_ch(), site, database=_ctx(database).database)


@router.post("/sites/{site_id}/ppc/keywords", status_code=201)
def add_ppc_keyword(
    site_id: int,
    body: PpcKeywordCreateIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    from app.api.v1.keywords import _ctx
    site = get_or_404(db, Site, site_id, "site")
    ctx = _ctx(body.database)
    dom = registrable_domain(host_of(body.domain if "://" in body.domain else f"https://{body.domain}")) or body.domain.removeprefix("www.")
    est_clicks = round(body.search_volume * ppc.paid_ctr(body.position), 1) if body.search_volume is not None else None
    est_cost = round(est_clicks * body.estimated_cpc, 2) if (est_clicks is not None and body.estimated_cpc is not None) else None
    lang = ctx.database.split("-")[0]
    rec = PpcKeywordRecord(
        org_id=p.org_id,
        site_id=site.id,
        domain=dom,
        keyword=body.keyword,
        keyword_key=match_key(body.keyword, lang),
        database=ctx.database,
        position=body.position,
        search_volume=body.search_volume,
        estimated_cpc=body.estimated_cpc,
        competition=body.competition,
        ad_headline=body.ad_headline,
        ad_description=body.ad_description,
        landing_page=body.landing_page,
        estimated_monthly_clicks=est_clicks,
        estimated_monthly_cost=est_cost,
        is_estimated=True,
        data_source="SERP_Paid_Ads_Observer",
        source_class="OBSERVED",
        confidence_score=0.8,
    )
    db.add(rec)
    db.commit()
    return ppc._ppc_out(rec)
