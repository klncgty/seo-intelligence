"""Module 8: Link Building & Outreach CRM API."""

from datetime import UTC, datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, get_or_404
from app.auth import Principal, current_principal, get_db
from app.models import LinkProspect, Site
from app.services import link_building as lb
from app.services.crawler.urls import host_of

router = APIRouter()


def _optional_ch():
    try:
        from app.api.v1.keywords import _ch
        return _ch()
    except Exception:
        return None


class ProspectCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str = Field(min_length=5, max_length=2048)
    target_url: str | None = Field(None, max_length=2048)
    link_type: str = Field("resource_page", pattern="^(competitor_link|link_gap|broken_link|unlinked_mention|resource_page|guest_post|relevant_site)$")
    relevance_score: float = Field(0.7, ge=0.0, le=1.0)
    domain_strength: float | None = Field(None, ge=0.0, le=100.0)


class ProspectUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outreach_status: str | None = None
    note: str | None = Field(None, max_length=2000)
    custom_angle: str | None = Field(None, max_length=1000)
    approve_draft: bool | None = None


@router.get("/sites/{site_id}/link-prospects")
def list_prospects(
    site_id: int,
    status: str | None = None,
    link_type: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    get_or_404(db, Site, site_id, "site")
    stmt = select(LinkProspect).where(LinkProspect.site_id == site_id, LinkProspect.org_id == p.org_id)
    if status:
        stmt = stmt.where(LinkProspect.outreach_status == status.upper())
    if link_type:
        stmt = stmt.where(LinkProspect.link_type == link_type)
    rows = db.scalars(stmt.order_by(LinkProspect.opportunity_score.desc(), LinkProspect.id.desc()).limit(200)).all()
    return {
        "site_id": site_id,
        "total": len(rows),
        "items": [lb._prospect_out(r) for r in rows],
    }


@router.post("/sites/{site_id}/link-prospects/discover")
def discover_prospects(site_id: int, db: Session = Depends(get_db), _: Principal = Depends(current_principal)):
    site = get_or_404(db, Site, site_id, "site")
    return lb.discover_prospects(db, _optional_ch(), site)


@router.post("/sites/{site_id}/link-prospects", status_code=201)
def create_prospect(
    site_id: int,
    body: ProspectCreateIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    site = get_or_404(db, Site, site_id, "site")
    dom = host_of(body.source_url).removeprefix("www.")
    if not dom:
        raise ApiError(422, "validation_error", "invalid source_url")
    opp_score = round(min(100.0, (body.domain_strength or 40.0) * 0.55 + body.relevance_score * 45.0), 1)
    rec = LinkProspect(
        org_id=p.org_id,
        site_id=site.id,
        domain=dom,
        source_url=body.source_url,
        target_url=body.target_url or site.base_url,
        link_type=body.link_type,
        relevance_score=body.relevance_score,
        domain_strength=body.domain_strength,
        opportunity_score=opp_score,
        contact_info={"emails": [], "contact_pages": [], "social_profiles": [], "status": "NOT_SCANNED"},
        outreach_status="PROSPECT",
        data_source="Manual_Or_Crawler",
        source_class="OBSERVED",
        confidence_score=0.85,
    )
    db.add(rec)
    db.commit()
    return lb._prospect_out(rec)


@router.post("/link-prospects/{prospect_id}/scan-contact")
async def scan_contact(prospect_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    rec = get_or_404(db, LinkProspect, prospect_id, "link prospect")
    if rec.org_id != p.org_id:
        raise ApiError(404, "not_found", "link prospect not found")
    return await lb.discover_public_contact(db, rec)


@router.post("/link-prospects/{prospect_id}/draft")
def generate_draft(
    prospect_id: int,
    body: ProspectUpdateIn | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    rec = get_or_404(db, LinkProspect, prospect_id, "link prospect")
    if rec.org_id != p.org_id:
        raise ApiError(404, "not_found", "link prospect not found")
    site = get_or_404(db, Site, rec.site_id, "site")
    return lb.generate_outreach_draft(db, rec, site, custom_angle=body.custom_angle if body else None)


@router.patch("/link-prospects/{prospect_id}")
def update_prospect(
    prospect_id: int,
    body: ProspectUpdateIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    rec = get_or_404(db, LinkProspect, prospect_id, "link prospect")
    if rec.org_id != p.org_id:
        raise ApiError(404, "not_found", "link prospect not found")
    if body.outreach_status is not None:
        st = body.outreach_status.upper()
        if st not in lb.VALID_STATUSES:
            raise ApiError(422, "validation_error", f"invalid status {body.outreach_status}")
        rec.outreach_status = st
    if body.approve_draft is not None and rec.email_draft:
        rec.email_draft = {
            **rec.email_draft,
            "user_approved": bool(body.approve_draft),
            "approved_at": datetime.now(UTC).isoformat() if body.approve_draft else None,
        }
    if body.note:
        hist = list(rec.outreach_history or [])
        hist.append({"timestamp": datetime.now(UTC).isoformat(), "status": rec.outreach_status, "note": body.note})
        rec.outreach_history = hist
    db.commit()
    return lb._prospect_out(rec)
