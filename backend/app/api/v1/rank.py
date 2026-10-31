"""SERP & Rank Tracking API (spec §6)."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, Page, get_or_404, paginate
from app.api.v1.keywords import _ch, _ctx
from app.auth import Principal, audit, current_principal, get_db
from app.models import Project, RankAlert, RankProject, SearchPerformanceDaily, TrackedKeyword
from app.normalize.domains import registrable_domain
from app.normalize.locale import DEFAULT_DATABASE, DEVICES
from app.normalize.text import match_key, normalize_keyword
from app.services import rank_tracking as rt

router = APIRouter()
MAX_COMPETITORS = 10


class RankProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    own_domain: str
    competitor_domains: list[str] = Field(default_factory=list, max_length=MAX_COMPETITORS)
    database: str = DEFAULT_DATABASE
    devices: list[str] = Field(default_factory=lambda: ["desktop"], min_length=1, max_length=2)
    locations: list[str] = Field(default_factory=lambda: [""], min_length=1, max_length=20)
    frequency: str = Field("daily", pattern="^(daily|weekly)$")
    project_id: int | None = None

    @field_validator("own_domain")
    @classmethod
    def _own(cls, v):
        d = registrable_domain(v.replace("https://", "").replace("http://", "").split("/")[0])
        if not d:
            raise ValueError("invalid domain")
        return d

    @field_validator("competitor_domains")
    @classmethod
    def _comps(cls, v):
        out = []
        for x in v:
            d = registrable_domain(x.replace("https://", "").replace("http://", "").split("/")[0])
            if not d:
                raise ValueError(f"invalid domain {x!r}")
            out.append(d)
        return list(dict.fromkeys(out))

    @field_validator("devices")
    @classmethod
    def _dev(cls, v):
        if any(d not in DEVICES for d in v):
            raise ValueError(f"devices must be in {DEVICES}")
        return list(dict.fromkeys(v))


def _out(p: RankProject, n: int | None = None) -> dict:
    return {"id": p.id, "name": p.name, "own_domain": p.own_domain, "competitor_domains": p.competitor_domains,
            "database": p.database, "devices": p.devices, "locations": p.locations, "frequency": p.frequency,
            "budget_confirmed": p.budget_confirmed, "is_active": p.is_active, "keyword_count": n,
            "last_check_date": p.last_check_date.isoformat() if p.last_check_date else None}


@router.post("/rank-projects", status_code=201)
def create(body: RankProjectIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ctx = _ctx(body.database)
    pid = body.project_id or db.scalar(select(Project.id).order_by(Project.id).limit(1))
    if pid is None:
        raise ApiError(422, "validation_error", "project not found")
    rp = RankProject(org_id=p.org_id, project_id=pid, name=body.name, own_domain=body.own_domain,
                     competitor_domains=[d for d in body.competitor_domains if d != body.own_domain], database=ctx.database,
                     devices=body.devices, locations=body.locations, frequency=body.frequency, budget_confirmed=False)
    db.add(rp)
    db.flush()
    audit(db, "rank_project.create", f"rank_project:{rp.id}")
    db.commit()
    return _out(rp, 0)


@router.get("/rank-projects")
def list_projects(page: Page = Depends(), db: Session = Depends(get_db)):
    return paginate(db, select(RankProject).order_by(RankProject.id), page, _out)


class KeywordsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    important: bool = False
    target_url: str | None = Field(None, max_length=2048)


@router.post("/rank-projects/{rp_id}/keywords")
def add_keywords(rp_id: int, body: KeywordsIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    lang = rp.database.split("-")[0]
    have = set(db.scalars(select(TrackedKeyword.keyword_key).where(TrackedKeyword.rank_project_id == rp.id)))
    added = 0
    for kw in body.keywords:
        norm = normalize_keyword(kw, lang)
        key = match_key(norm, lang)
        if norm and key not in have:
            have.add(key)
            db.add(TrackedKeyword(org_id=p.org_id, rank_project_id=rp.id, keyword=norm[:500], keyword_key=key[:500],
                                  tags=body.tags, important=body.important, target_url=body.target_url))
            added += 1
    rp.budget_confirmed = False  # cost changed → recurring estimate must be re-confirmed
    db.commit()
    n = db.scalar(select(func.count()).select_from(TrackedKeyword).where(TrackedKeyword.rank_project_id == rp.id))
    return {"added": added, "total_keywords": n, "monthly_estimate": rt.monthly_estimate(rp, n), "budget_confirmed": False}


@router.get("/rank-projects/{rp_id}/estimate")
def estimate(rp_id: int, db: Session = Depends(get_db)):
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    n = db.scalar(select(func.count()).select_from(TrackedKeyword).where(TrackedKeyword.rank_project_id == rp.id))
    return {**rt.monthly_estimate(rp, n), "per_check_serp_requests": rt.checks_per_run(rp, n)}


@router.post("/rank-projects/{rp_id}/confirm-budget")
def confirm_budget(rp_id: int, db: Session = Depends(get_db)):
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    rp.budget_confirmed = True
    rt.ensure_schedule(db, rp)
    audit(db, "rank_project.confirm_budget", f"rank_project:{rp.id}")
    db.commit()
    return _out(rp)


@router.post("/rank-projects/{rp_id}/check", status_code=202)
def check(rp_id: int, db: Session = Depends(get_db)):
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    if not rp.budget_confirmed:
        raise ApiError(428, "confirmation_required", "confirm the recurring credit estimate first (POST confirm-budget)")
    return rt.enqueue_check(db, rp)


@router.get("/rank-projects/{rp_id}/overview")
def overview(rp_id: int, db: Session = Depends(get_db)):
    return rt.overview(db, _ch(), get_or_404(db, RankProject, rp_id, "rank project"))


@router.get("/rank-projects/{rp_id}/rankings")
def rankings(rp_id: int, device: str | None = Query(None), location: str | None = Query(None),
             db: Session = Depends(get_db)):
    return rt.keyword_rankings_table(db, _ch(), get_or_404(db, RankProject, rp_id, "rank project"),
                                     device=device, location=location)


@router.get("/rank-projects/{rp_id}/keywords")
def keywords(rp_id: int, page: Page = Depends(), db: Session = Depends(get_db)):
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    return paginate(db, select(TrackedKeyword).where(TrackedKeyword.rank_project_id == rp.id).order_by(TrackedKeyword.id), page,
                    lambda k: {"id": k.id, "keyword": k.keyword, "tags": k.tags, "important": k.important, "target_url": k.target_url})


@router.get("/rank-projects/{rp_id}/keywords/{kid}/history")
def keyword_history(rp_id: int, kid: int, db: Session = Depends(get_db)):
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    k = db.get(TrackedKeyword, kid)
    if k is None or k.rank_project_id != rp.id:
        raise ApiError(404, "not_found", "tracked keyword not found")
    return {"keyword": k.keyword, "source_class": "PROVIDER", "history": rt.history(_ch(), rp.org_id, rp.id, kid)}


@router.get("/rank-projects/{rp_id}/gsc-comparison")
def gsc_comparison(rp_id: int, days: int = Query(7, ge=1, le=90), db: Session = Depends(get_db)):
    """Tracked rank (PROVIDER snapshot) next to own GSC average position (FIRST_PARTY). Never merged."""
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    hist = rt.history(_ch(), rp.org_id, rp.id)
    latest = {}
    for r in hist:
        if r["domain"] == rp.own_domain:
            latest[r["keyword_key"]] = r
    since = date.today() - timedelta(days=days)
    lang = rp.database.split("-")[0]
    gsc = {}
    for q, impr, wpos in db.execute(select(SearchPerformanceDaily.query, func.sum(SearchPerformanceDaily.impressions),
                                           func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions))
                                    .where(SearchPerformanceDaily.date >= since).group_by(SearchPerformanceDaily.query)):
        gsc[match_key(q, lang)] = (int(impr), wpos / impr if impr else None)
    items = []
    for k in db.scalars(select(TrackedKeyword).where(TrackedKeyword.rank_project_id == rp.id).order_by(TrackedKeyword.id)):
        r = latest.get(k.keyword_key)
        g = gsc.get(k.keyword_key)
        items.append({"keyword": k.keyword,
                      "tracked_rank": {"value": r["position"] if r else None, "date": str(r["check_date"]) if r else None,
                                       "source_class": "PROVIDER", "device": r["device"] if r else None},
                      "gsc_avg_position": {"value": round(g[1], 1) if g and g[1] else None, "impressions": g[0] if g else None,
                                           "window_days": days, "source_class": "FIRST_PARTY"}})
    return {"items": items, "why_they_differ": "GSC averages every impression across devices, locations, personalization and "
            "dates; tracked rank is one non-personalized snapshot for a fixed device/location/day."}


@router.get("/rank-projects/{rp_id}/alerts")
def alerts(rp_id: int, page: Page = Depends(), db: Session = Depends(get_db)):
    rp = get_or_404(db, RankProject, rp_id, "rank project")
    return paginate(db, select(RankAlert).where(RankAlert.rank_project_id == rp.id).order_by(RankAlert.id.desc()), page,
                    lambda a: {"id": a.id, "type": a.alert_type, "keyword": a.keyword, "severity": a.severity,
                               "details": a.details, "created_at": a.created_at.isoformat()})


@router.get("/serp/volatility")
def serp_volatility(database: str = DEFAULT_DATABASE, device: str = "desktop", days: int = Query(30, ge=2, le=365),
                    db: Session = Depends(get_db), _: Principal = Depends(current_principal)):
    ctx = _ctx(database, device)
    return rt.volatility(_ch(), ctx.database, device, days, db)


class GeoGridIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=300)
    own_domain: str = Field(min_length=3, max_length=253)
    database: str = DEFAULT_DATABASE
    device: str = Field("mobile", pattern="^(desktop|mobile)$")
    center_lat: float = Field(ge=-85.0, le=85.0)
    center_lng: float = Field(ge=-180.0, le=180.0)
    grid_size: int = Field(3, ge=3, le=7)
    step_km: float = Field(2.0, gt=0.1, le=50.0)
    confirm: bool = False


@router.post("/serp/geo-grid")
async def geo_grid(body: GeoGridIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    import uuid
    from app.api.v1.keywords import _provider_errors
    ctx = _ctx(body.database, body.device)
    try:
        return await rt.geo_grid_check(
            db, _ch(), p.org_id, body.keyword, body.own_domain, ctx.database,
            body.center_lat, body.center_lng, body.grid_size, body.step_km,
            body.device, body.confirm, uuid.uuid4().hex,
        )
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    except Exception as exc:
        raise _provider_errors(exc) from exc
