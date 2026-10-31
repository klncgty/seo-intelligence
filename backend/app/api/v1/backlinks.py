"""Backlink intelligence API (spec §8)."""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import jobs
from app.api.v1.common import ApiError, get_or_404
from app.api.v1.domains import _domain
from app.api.v1.keywords import _ch, _provider_errors
from app.auth import Principal, current_principal, get_db, require_role
from app.models import Site
from app.services import backlinks as bl

router = APIRouter(prefix="/backlinks")


@router.get("/{domain}/overview")
def overview(domain: str, keywords: list[str] = Query(default=[]), _: Principal = Depends(current_principal)):
    return bl.overview(_ch(), _domain(domain), keywords[:200])


@router.get("/{domain}/referring-domains")
def referring(domain: str, limit: int = Query(200, ge=1, le=2000), _: Principal = Depends(current_principal)):
    ch = _ch()
    d = _domain(domain)
    return {"items": bl.referring_domains(ch, d, limit), "coverage": bl.coverage(ch, d)}


@router.get("/{domain}/new-lost")
def new_lost(domain: str, days: int = Query(30, ge=1, le=365), _: Principal = Depends(current_principal)):
    return bl.new_lost(_ch(), _domain(domain), days)


@router.get("/{domain}/intersect")
def intersect(domain: str, competitors: list[str] = Query(min_length=1, max_length=5), min_competitors: int = Query(2, ge=1, le=5),
              _: Principal = Depends(current_principal)):
    return bl.intersect(_ch(), _domain(domain), [_domain(c) for c in competitors], min_competitors)


@router.get("/{domain}/risky")
def risky(domain: str, keywords: list[str] = Query(default=[]), _: Principal = Depends(current_principal)):
    return bl.risky_links(_ch(), _domain(domain), keywords[:200])


@router.get("/{domain}/audit")
def audit_backlinks(domain: str, keywords: list[str] = Query(default=[]), _: Principal = Depends(current_principal)):
    return bl.risky_links(_ch(), _domain(domain), keywords[:200])


class DisavowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domains: list[str] = Field(default_factory=list, max_length=5000)
    urls: list[str] = Field(default_factory=list, max_length=5000)


@router.post("/disavow", response_class=PlainTextResponse)
def disavow(body: DisavowIn, _: Principal = Depends(require_role("editor"))):
    if not body.domains and not body.urls:
        raise ApiError(422, "validation_error", "at least one domain or url is required")
    return bl.disavow_file(body.domains, body.urls or None)


class FetchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(1000, ge=1, le=10000)
    confirm: bool = False


@router.post("/{domain}/fetch")
async def fetch(domain: str, body: FetchIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    try:
        return await bl.fetch_provider_backlinks(db, _ch(), p.org_id, _domain(domain), body.limit, body.confirm, uuid.uuid4().hex)
    except Exception as exc:
        raise _provider_errors(exc) from exc


@router.post("/{domain}/verify", status_code=202)
def verify(domain: str, limit: int = Query(200, ge=1, le=5000), db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    j = jobs.enqueue(db, "backlink_verify", {"domain": _domain(domain), "limit": limit}, tenant_id=p.org_id)
    return {"job_id": j.id}


@router.post("/domain-strength/recompute", status_code=202)
def recompute(db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    j = jobs.enqueue(db, "domain_strength", {}, tenant_id=p.org_id, idempotency_key=None)
    return {"job_id": j.id}


class CCIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=5, max_length=500, description="WAT path under data.commoncrawl.org")
    targets: list[str] = Field(min_length=1, max_length=1000, description="registrable domains or TLD suffixes like .tr")
    max_records: int = Field(200_000, ge=1, le=50_000_000)


@router.post("/common-crawl/import", status_code=202)
def cc_import(body: CCIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    if not (body.path.startswith("crawl-data/") and body.path.endswith(".warc.wat.gz")):
        raise ApiError(422, "validation_error", "path must be a Common Crawl WAT file (crawl-data/…/*.warc.wat.gz)")
    j = jobs.enqueue(db, "cc_import", body.model_dump(), tenant_id=p.org_id, idempotency_key=f"cc:{body.path}:{','.join(sorted(body.targets))}")
    return {"job_id": j.id}


@router.post("/sites/{site_id}/reclamation")
def reclamation(site_id: int, db: Session = Depends(get_db)):
    return bl.reclamation_opportunities(db, _ch(), get_or_404(db, Site, site_id, "site"))
