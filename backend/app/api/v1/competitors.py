"""Competitor Intelligence API. Competitors are always explicitly registered by the operator."""

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, Page, get_or_404, paginate
from app.auth import get_db
from app.models import Competitor, CrawlRun, Site
from app.schemas import CrawlCreate, CrawlSettings, crawl_out, iso
from app.services import competitor_service as cs
from app.services import crawl_service
from app.services.crawler.urls import host_of

router = APIRouter()


def _domain_to_url(v: str) -> str:
    v = v.strip()
    from app.schemas import _validate_base_url
    return _validate_base_url(v if "://" in v else f"https://{v}")


class CompetitorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=3, max_length=2048)
    notes: str | None = Field(None, max_length=5000)
    extra_hosts: list[str] = Field(default_factory=list, max_length=10)
    crawl_settings: CrawlSettings = Field(default_factory=CrawlSettings)

    @field_validator("domain")
    @classmethod
    def _d(cls, v):
        return _domain_to_url(v)


class CompetitorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=200)
    domain: str | None = Field(None, min_length=3, max_length=2048)
    notes: str | None = Field(None, max_length=5000)
    extra_hosts: list[str] | None = Field(None, max_length=10)
    crawl_settings: CrawlSettings | None = None
    is_active: bool | None = None

    @field_validator("domain")
    @classmethod
    def _d(cls, v):
        return _domain_to_url(v) if v is not None else v


def _hosts(base_url: str, extra: list[str]) -> list[str]:
    from app.schemas import _validate_hosts
    host = host_of(base_url)
    bare = host[4:] if host.startswith("www.") else host
    return list(dict.fromkeys([host, bare, f"www.{bare}", *_validate_hosts(extra)]))


def competitor_out(c: Competitor, last_crawl: CrawlRun | None = None) -> dict:
    return {"id": c.id, "site_id": c.site_id, "name": c.name, "domain": c.domain, "base_url": c.base_url,
            "allowed_hosts": c.allowed_hosts, "notes": c.notes, "crawl_settings": c.crawl_settings,
            "is_active": c.is_active, "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
            "last_crawl": crawl_out(last_crawl) if last_crawl else None}


def _get(db: Session, site_id: int, comp_id: int) -> Competitor:
    c = db.get(Competitor, comp_id)
    if c is None or c.site_id != site_id:
        raise ApiError(404, "not_found", "competitor not found")
    return c


@router.post("/sites/{site_id}/competitors", status_code=201)
def create_competitor(site_id: int, body: CompetitorCreate, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    if host_of(body.domain).removeprefix("www.") == host_of(site.base_url).removeprefix("www."):
        raise ApiError(422, "validation_error", "competitor domain cannot be the site itself")
    c = Competitor(site_id=site_id, name=body.name, domain=host_of(body.domain).removeprefix("www."),
                   base_url=body.domain, allowed_hosts=_hosts(body.domain, body.extra_hosts), notes=body.notes,
                   crawl_settings=body.crawl_settings.model_dump(exclude_none=True))
    db.add(c)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "conflict", "competitor already registered for this site") from exc
    return competitor_out(c)


@router.get("/sites/{site_id}/competitors")
def list_competitors(site_id: int, page: Page = Depends(), db: Session = Depends(get_db)):
    get_or_404(db, Site, site_id, "site")
    stmt = select(Competitor).where(Competitor.site_id == site_id).order_by(Competitor.id)
    return paginate(db, stmt, page, lambda c: competitor_out(c, cs.last_crawl(db, c.id)))


def _optional_ch():
    try:
        from app.api.v1.keywords import _ch
        return _ch()
    except Exception:
        return None


@router.get("/sites/{site_id}/competitors/suggestions")
def suggestions(site_id: int, limit: int = Query(20, ge=1, le=100), database: str = "tr-TR:google.com.tr", db: Session = Depends(get_db)):
    from app.api.v1.keywords import _ctx
    site = get_or_404(db, Site, site_id, "site")
    return cs.suggest_competitors(db, site, limit, ch=_optional_ch(), database=_ctx(database).database)


@router.get("/sites/{site_id}/competitors/{comp_id}")
def get_competitor(site_id: int, comp_id: int, db: Session = Depends(get_db)):
    c = _get(db, site_id, comp_id)
    return competitor_out(c, cs.last_crawl(db, c.id))


@router.patch("/sites/{site_id}/competitors/{comp_id}")
def update_competitor(site_id: int, comp_id: int, body: CompetitorUpdate, db: Session = Depends(get_db)):
    c = _get(db, site_id, comp_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("domain"):
        c.base_url, c.domain = data["domain"], host_of(data["domain"]).removeprefix("www.")
    if "domain" in data or "extra_hosts" in data:
        extra = data.get("extra_hosts") or [h for h in c.allowed_hosts if host_of("https://" + h).removeprefix("www.") != c.domain]
        c.allowed_hosts = _hosts(c.base_url, extra if "extra_hosts" in data else [])
    for k in ("name", "notes", "is_active"):
        if k in data:
            setattr(c, k, data[k])
    if body.crawl_settings is not None:
        c.crawl_settings = {**(c.crawl_settings or {}), **body.crawl_settings.model_dump(exclude_none=True)}
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "conflict", "competitor already registered for this site") from exc
    return competitor_out(c, cs.last_crawl(db, c.id))


@router.delete("/sites/{site_id}/competitors/{comp_id}", status_code=204)
def delete_competitor(site_id: int, comp_id: int, db: Session = Depends(get_db)):
    db.delete(_get(db, site_id, comp_id))
    db.commit()
    return Response(status_code=204)


@router.post("/sites/{site_id}/competitors/{comp_id}/crawls", status_code=202)
def crawl_competitor(site_id: int, comp_id: int, body: CrawlCreate | None = None, db: Session = Depends(get_db)):
    c = _get(db, site_id, comp_id)
    if not c.is_active:
        raise ApiError(409, "conflict", "competitor is inactive")
    crawl_service.lock_site(db, site_id)
    if crawl_service.active_crawl_exists(db, site_id, c.id):
        raise ApiError(409, "conflict", "a crawl is already queued or running for this competitor")
    overrides = body.crawl_settings.model_dump(exclude_none=True) if body else {}
    run = CrawlRun(site_id=site_id, competitor_id=c.id, status="queued",
                   config=crawl_service.effective_crawl_config(c, overrides, competitor=True))
    db.add(run)
    db.commit()
    return crawl_out(run)


@router.get("/sites/{site_id}/competitors/{comp_id}/crawls")
def competitor_crawls(site_id: int, comp_id: int, page: Page = Depends(), db: Session = Depends(get_db)):
    c = _get(db, site_id, comp_id)
    return paginate(db, select(CrawlRun).where(CrawlRun.competitor_id == c.id).order_by(CrawlRun.id.desc()), page, crawl_out)


@router.get("/sites/{site_id}/competitors-comparison")
def comparison(site_id: int, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    return cs.comparison(db, site)


@router.get("/sites/{site_id}/competitors-content-gaps")
def content_gaps(site_id: int, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    return cs.content_gaps(db, site, limit)


@router.post("/sites/{site_id}/competitors-opportunities/generate")
def generate_competitor_opportunities(site_id: int, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    return cs.generate_opportunities(db, site)


@router.get("/seo-data/status")
def provider_status():
    return cs.provider_status()


@router.post("/sites/{site_id}/competitors-keyword-gap")
async def keyword_gap(site_id: int, limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    return await cs.keyword_gap(db, get_or_404(db, Site, site_id, "site"), limit)


@router.post("/sites/{site_id}/competitors-backlinks")
async def backlinks(site_id: int, db: Session = Depends(get_db)):
    return await cs.backlinks(db, get_or_404(db, Site, site_id, "site"))


@router.get("/sites/{site_id}/external-seo-data")
def external_data(site_id: int, kind: str | None = None, db: Session = Depends(get_db)):
    get_or_404(db, Site, site_id, "site")
    return cs.external_snapshots(db, site_id, kind)


@router.get("/sites/{site_id}/competitors-traffic")
def competitors_traffic(site_id: int, database: str = "tr-TR:google.com.tr", db: Session = Depends(get_db)):
    from app.api.v1.keywords import _ctx
    site = get_or_404(db, Site, site_id, "site")
    return cs.competitor_traffic(db, _optional_ch(), site, database=_ctx(database).database)


@router.get("/sites/{site_id}/competitors-matrix")
def competitors_matrix(site_id: int, database: str = "tr-TR:google.com.tr", db: Session = Depends(get_db)):
    from app.api.v1.keywords import _ctx
    site = get_or_404(db, Site, site_id, "site")
    return cs.competitor_matrix(db, _optional_ch(), site, database=_ctx(database).database)
