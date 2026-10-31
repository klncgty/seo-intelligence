"""Keyword Intelligence API (spec §5)."""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, Page, get_or_404, paginate
from app.auth import Principal, audit, current_principal, get_db
from app.infra.clickhouse import ClickHouse
from app.models import KeywordList, KeywordListItem, Project, RankProject, Site, TrackedKeyword
from app.normalize.locale import DEFAULT_DATABASE, SearchContext
from app.normalize.text import match_key, normalize_keyword
from app.providers.base import BudgetExceeded, CircuitOpen, ProviderError, ProviderNotConfigured
from app.services import credits
from app.services.keywords import models, service, store

router = APIRouter(prefix="/keywords")
lists_router = APIRouter(prefix="/keyword-lists")


def _ch() -> ClickHouse:
    ch = ClickHouse()
    if not ch.configured or not ch.ping():
        raise ApiError(503, "analytics_unavailable", "ClickHouse (analytics profile) is not configured or unreachable",
                       {"setup": "set CLICKHOUSE_URL and run `seo ch-migrate` (scripts/dev-services.sh start)"})
    return ch


def _ctx(database: str, device: str = "desktop", location: str = "") -> SearchContext:
    try:
        return SearchContext(database, device, location)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc


def _provider_errors(exc: Exception) -> ApiError:
    if isinstance(exc, service.ConfirmationRequired):
        return ApiError(428, "confirmation_required", "re-send with confirm=true to accept the credit estimate", exc.preflight)
    if isinstance(exc, ProviderNotConfigured):
        return ApiError(400, "provider_not_configured", str(exc))
    if isinstance(exc, credits.InsufficientCredits):
        return ApiError(402, "insufficient_credits", str(exc))
    if isinstance(exc, (BudgetExceeded, CircuitOpen)):
        return ApiError(503, "provider_unavailable", str(exc))
    if isinstance(exc, ProviderError):
        return ApiError(502, "provider_error", str(exc))
    raise exc


@router.get("/overview")
def overview(keyword: str = Query(min_length=1, max_length=300), database: str = DEFAULT_DATABASE, device: str = "desktop",
             location: str = "", db: Session = Depends(get_db)):
    return service.overview(db, _ch(), _ctx(database, device, location), keyword)


@router.get("/explorer")
def explorer(database: str = DEFAULT_DATABASE, q: str | None = Query(None, max_length=300),
             match: str = Query("broad", pattern="^(broad|phrase|exact)$"), min_volume: int | None = Query(None, ge=0),
             max_volume: int | None = Query(None, ge=0), min_kd: float | None = Query(None, ge=0, le=100),
             max_kd: float | None = Query(None, ge=0, le=100), min_cpc: float | None = Query(None, ge=0),
             intent: str | None = Query(None, pattern="^(informational|commercial|transactional|navigational)$"),
             feature: str | None = Query(None, max_length=50), min_words: int | None = Query(None, ge=1, le=20),
             max_words: int | None = Query(None, ge=1, le=20), question: bool | None = None,
             include: list[str] = Query(default=[]), exclude: list[str] = Query(default=[]),
             sort: str = Query("volume", pattern="^(volume|kd|cpc|keyword)$"), page: Page = Depends(),
             _: Principal = Depends(current_principal)):
    ctx = _ctx(database)
    res = store.explorer(_ch(), ctx.database, q=q, match=match, min_volume=min_volume, max_volume=max_volume, min_kd=min_kd,
                         max_kd=max_kd, min_cpc=min_cpc, intent=intent, feature=feature, min_words=min_words,
                         max_words=max_words, question=question, include=include[:10], exclude=exclude[:10], sort=sort,
                         limit=page.page_size, offset=page.offset)
    return {**res, "page": page.page, "page_size": page.page_size, "database": ctx.database,
            "note": "Global dataset (PROVIDER/DEMO). Keywords appear only after a provider lookup."}


class LookupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = DEFAULT_DATABASE
    device: str = "desktop"
    location: str = Field("", max_length=200)
    keywords: list[str] = Field(min_length=1, max_length=1000)
    confirm: bool = False
    request_id: str | None = Field(None, max_length=64)


@router.post("/lookup")
async def lookup(body: LookupIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ch = _ch()
    try:
        out = await service.lookup_metrics(db, ch, p.org_id, _ctx(body.database), body.keywords, body.confirm,
                                           body.request_id or uuid.uuid4().hex)
    except Exception as exc:
        raise _provider_errors(exc) from exc
    audit(db, "keywords.lookup", None, {"count": len(body.keywords), "database": body.database})
    db.commit()
    return out


@router.post("/serps")
async def serps(body: LookupIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ch = _ch()
    try:
        return await service.fetch_serps(db, ch, p.org_id, _ctx(body.database, body.device, body.location), body.keywords,
                                         body.confirm, body.request_id or uuid.uuid4().hex)
    except Exception as exc:
        raise _provider_errors(exc) from exc


@router.get("/first-party")
def first_party(seed: str | None = Query(None, max_length=300), database: str = DEFAULT_DATABASE, db: Session = Depends(get_db)):
    return service.first_party_queries(db, seed, _ctx(database).locale.language)


class ClusterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = DEFAULT_DATABASE
    keywords: list[str] = Field(min_length=1, max_length=2000)
    min_shared: int = Field(3, ge=1, le=10)
    site_id: int | None = None


@router.post("/clusters")
def clusters(body: ClusterIn, db: Session = Depends(get_db)):
    ctx = _ctx(body.database)
    if body.site_id is None:
        site = db.scalar(select(Site).order_by(Site.id).limit(1))
        if site is None:
            raise ApiError(422, "validation_error", "site_id required (no site in this organization)")
    else:
        site = get_or_404(db, Site, body.site_id, "site")
    return service.strategy(db, _ch(), ctx, site, body.keywords, body.min_shared)


@router.post("/intent")
def intent(body: ClusterIn, _: Principal = Depends(current_principal)):
    lang = _ctx(body.database).locale.language
    return {"items": [{"keyword": k, **models.classify_intent(k, lang)} for k in body.keywords[:500]],
            "note": "Rule-based (modifiers only); SERP composition adds signals once a snapshot exists."}


@router.get("/kd-validation")
def kd_validation(database: str = DEFAULT_DATABASE, p: Principal = Depends(current_principal)):
    ctx = _ctx(database)
    return service.validate_kd_vs_rank(_ch(), p.org_id, ctx.database)


@router.get("/magic")
def magic(seed: str = Query(min_length=1, max_length=300), database: str = DEFAULT_DATABASE,
          match: str = Query("broad", pattern="^(broad|phrase|exact)$"),
          min_volume: int | None = Query(None, ge=0), max_volume: int | None = Query(None, ge=0),
          min_kd: float | None = Query(None, ge=0, le=100), max_kd: float | None = Query(None, ge=0, le=100),
          min_cpc: float | None = Query(None, ge=0),
          intent: str | None = Query(None, pattern="^(informational|commercial|transactional|navigational)$"),
          question: bool | None = None,
          include: list[str] = Query(default=[]), exclude: list[str] = Query(default=[]),
          _: Principal = Depends(current_principal)):
    ctx = _ctx(database)
    return service.magic_tool(_ch(), ctx, seed, match=match, min_volume=min_volume, max_volume=max_volume,
                              min_kd=min_kd, max_kd=max_kd, min_cpc=min_cpc, intent=intent, question=question,
                              include=include[:10], exclude=exclude[:10])


class CompareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = DEFAULT_DATABASE
    device: str = "desktop"
    location: str = Field("", max_length=200)
    keywords: list[str] = Field(min_length=2, max_length=50)


@router.post("/compare")
def compare(body: CompareIn, _: Principal = Depends(current_principal)):
    return service.compare_keywords(_ch(), _ctx(body.database, body.device, body.location), body.keywords)


@router.get("/history")
def history(database: str = DEFAULT_DATABASE, keyword: str | None = Query(None, max_length=300),
            limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
            p: Principal = Depends(current_principal)):
    return service.keyword_history(db, _ch(), p.org_id, _ctx(database).database, keyword=keyword, limit=limit)


@router.get("/export")
def export_keywords(database: str = DEFAULT_DATABASE, q: str | None = Query(None, max_length=300),
                    format: str = Query("csv", pattern="^(csv|json)$"),
                    _: Principal = Depends(current_principal)):
    ctx = _ctx(database)
    res = store.explorer(_ch(), ctx.database, q=q, limit=1000, offset=0)
    if format == "json":
        return {"database": ctx.database, "total": res["total"], "items": res["items"]}
    csv_text = service.format_keywords_csv(res["items"])
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="keywords.csv"'})


# ---------------------------------------------------------------- keyword lists

class ListIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    database: str = DEFAULT_DATABASE
    project_id: int | None = None


class ItemsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    target_url: str | None = Field(None, max_length=2048)


def _list_out(k: KeywordList, count: int | None = None) -> dict:
    return {"id": k.id, "name": k.name, "database": k.database, "project_id": k.project_id, "keyword_count": count}


@lists_router.get("")
def list_lists(page: Page = Depends(), db: Session = Depends(get_db)):
    return paginate(db, select(KeywordList).order_by(KeywordList.id), page, _list_out)


@lists_router.post("", status_code=201)
def create_list(body: ListIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    _ctx(body.database)
    pid = body.project_id or db.scalar(select(Project.id).order_by(Project.id).limit(1))
    if pid is None or db.get(Project, pid) is None:
        raise ApiError(422, "validation_error", "project not found")
    k = KeywordList(org_id=p.org_id, project_id=pid, name=body.name, database=body.database)
    db.add(k)
    db.commit()
    return _list_out(k, 0)


@lists_router.get("/{list_id}")
def get_list(list_id: int, page: Page = Depends(), db: Session = Depends(get_db)):
    k = get_or_404(db, KeywordList, list_id, "keyword list")
    items = paginate(db, select(KeywordListItem).where(KeywordListItem.list_id == k.id).order_by(KeywordListItem.id), page,
                     lambda i: {"id": i.id, "keyword": i.keyword, "tags": i.tags, "target_url": i.target_url})
    return {**_list_out(k, items["total"]), "items": items}


@lists_router.post("/{list_id}/items")
def add_items(list_id: int, body: ItemsIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    k = get_or_404(db, KeywordList, list_id, "keyword list")
    lang = k.database.split("-")[0]
    existing = set(db.scalars(select(KeywordListItem.keyword_key).where(KeywordListItem.list_id == k.id)))
    added = 0
    for kw in body.keywords:
        norm = normalize_keyword(kw, lang)
        key = match_key(norm, lang)
        if not norm or key in existing:
            continue
        existing.add(key)
        db.add(KeywordListItem(org_id=p.org_id, list_id=k.id, keyword=norm[:500], keyword_key=key[:500], tags=body.tags,
                               target_url=body.target_url))
        added += 1
    db.commit()
    return {"added": added, "skipped_duplicates": len(body.keywords) - added}


@lists_router.delete("/{list_id}")
def delete_list(list_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, KeywordList, list_id, "keyword list"))
    db.commit()
    return {"deleted": True}


@lists_router.get("/{list_id}/export")
def export_list(list_id: int, format: str = Query("csv", pattern="^(csv|json)$"),
                db: Session = Depends(get_db)):
    k = get_or_404(db, KeywordList, list_id, "keyword list")
    items = db.scalars(select(KeywordListItem).where(KeywordListItem.list_id == k.id).order_by(KeywordListItem.id)).all()
    ch = ClickHouse()
    metrics_by_key: dict[str, dict] = {}
    if ch.configured and ch.ping() and items:
        metrics_by_key = {
            r["keyword_key"]: r
            for r in store.explorer(ch, k.database, limit=5000)["items"]
        }
    rows = []
    for i in items:
        m = metrics_by_key.get(i.keyword_key) or {}
        rows.append({
            "keyword": i.keyword,
            "keyword_key": i.keyword_key,
            "volume": m.get("volume"),
            "kd": m.get("kd"),
            "cpc": m.get("cpc"),
            "competition": m.get("competition"),
            "intents": m.get("intents") or [],
            "serp_features": m.get("features") or [],
            "source_class": m.get("source_class", "OBSERVED"),
            "fetched_at": m.get("fetched_at"),
            "tags": i.tags,
            "target_url": i.target_url,
        })
    if format == "json":
        return {"list": _list_out(k, len(rows)), "items": rows}
    csv_text = service.format_keywords_csv(rows)
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="keyword-list-{k.id}.csv"'})


class SendToRankIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank_project_id: int


@lists_router.post("/{list_id}/send-to-rank")
def send_to_rank(list_id: int, body: SendToRankIn, db: Session = Depends(get_db),
                 p: Principal = Depends(current_principal)):
    k = get_or_404(db, KeywordList, list_id, "keyword list")
    rp = get_or_404(db, RankProject, body.rank_project_id, "rank project")
    items = db.scalars(select(KeywordListItem).where(KeywordListItem.list_id == k.id).order_by(KeywordListItem.id)).all()
    existing = set(db.scalars(select(TrackedKeyword.keyword_key).where(TrackedKeyword.rank_project_id == rp.id)))
    added = 0
    for item in items:
        if item.keyword_key in existing:
            continue
        existing.add(item.keyword_key)
        db.add(TrackedKeyword(
            org_id=p.org_id,
            rank_project_id=rp.id,
            keyword=item.keyword,
            keyword_key=item.keyword_key,
            tags=item.tags or [],
            target_url=item.target_url,
        ))
        added += 1
    db.commit()
    return {"rank_project_id": rp.id, "added": added, "skipped_duplicates": len(items) - added}
