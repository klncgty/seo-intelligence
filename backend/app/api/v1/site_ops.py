"""Site operations API: verification, logs, migration, CWV, URL Inspection, GSC sitemaps (spec §9–§10)."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, get_or_404
from app.auth import audit, get_db
from app.models import Site
from app.services import google_service, site_ops

router = APIRouter(prefix="/sites/{site_id}")


@router.post("/verification")
def start_verification(site_id: int, db: Session = Depends(get_db)):
    return site_ops.start_verification(db, get_or_404(db, Site, site_id, "site"))


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(pattern="^(dns_txt|html_meta|file|gsc)$")


@router.post("/verification/check")
async def check_verification(site_id: int, body: VerifyIn, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    try:
        res = await site_ops.check_verification(db, site, body.method)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    except google_service.IntegrationNotConfigured as exc:
        raise ApiError(400, "integration_not_configured", str(exc)) from exc
    if res["verified"]:
        audit(db, "site.verified", f"site:{site.id}", {"method": body.method})
        db.commit()
    return res


@router.post("/logs")
async def upload_logs(site_id: int, request: Request, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    if site.verified_at is None:
        raise ApiError(403, "verification_required", "log upload requires a verified domain")
    size, buf, lines = 0, b"", []
    async for chunk in request.stream():
        size += len(chunk)
        if size > site_ops.MAX_LOG_BYTES:
            raise ApiError(413, "too_large", f"log upload limited to {site_ops.MAX_LOG_BYTES} bytes")
        buf += chunk
        *complete, buf = buf.split(b"\n")
        lines.extend(complete)
    if buf:
        lines.append(buf)
    return site_ops.ingest_log(db, site, lines)


@router.get("/logs/report")
def logs_report(site_id: int, days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    return site_ops.log_report(db, get_or_404(db, Site, site_id, "site"), days)


class MigrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv: str = Field(min_length=3, max_length=2_000_000)


@router.post("/migration/validate")
async def migration_validate(site_id: int, body: MigrationIn, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    mapping = site_ops.parse_redirect_map(body.csv, limit=500)
    if not mapping:
        raise ApiError(422, "validation_error", "CSV must contain from,to URL rows")
    return await site_ops.validate_migration(site, mapping)


@router.get("/cwv")
async def cwv(site_id: int, form_factor: str = Query("PHONE", pattern="^(PHONE|DESKTOP|TABLET)$"),
              lab: bool = False, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    origin = site.base_url.rstrip("/")
    field = {"origin": await site_ops.crux(origin, "origin", form_factor),
             "history": await site_ops.crux(origin, "origin", form_factor, history=True)}
    reps = site_ops.template_representatives(db, site)
    out = {"field": field, "template_representatives": reps,
           "note": "Field (CrUX real users) and lab (PSI/Lighthouse) are different measurements and never mixed."}
    if lab:
        out["lab"] = [await site_ops.psi(u, "mobile" if form_factor == "PHONE" else "desktop") for u in reps[:5]]
    return out


class InspectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[str] = Field(min_length=1, max_length=200)


@router.post("/url-inspection")
async def url_inspection(site_id: int, body: InspectIn, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    try:
        return await site_ops.inspect_urls(db, site, body.urls)
    except google_service.IntegrationNotConfigured as exc:
        raise ApiError(400, "integration_not_configured", str(exc)) from exc


@router.get("/gsc-sitemaps")
async def gsc_sitemaps(site_id: int, db: Session = Depends(get_db)):
    try:
        return await site_ops.gsc_sitemaps(db, get_or_404(db, Site, site_id, "site"))
    except google_service.IntegrationNotConfigured as exc:
        raise ApiError(400, "integration_not_configured", str(exc)) from exc
