"""Module 11: AI SEO Diagnosis & Opportunity Engine API."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, get_or_404
from app.auth import Principal, current_principal, get_db
from app.models import SeoDiagnosisRecord, Site
from app.services import diagnosis

router = APIRouter()


class StatusUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(new|planned|in_progress|completed|ignored)$")


@router.get("/sites/{site_id}/seo-diagnoses")
def get_diagnoses(
    site_id: int,
    category: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    get_or_404(db, Site, site_id, "site")
    stmt = select(SeoDiagnosisRecord).where(SeoDiagnosisRecord.site_id == site_id, SeoDiagnosisRecord.org_id == p.org_id)
    if category:
        stmt = stmt.where(SeoDiagnosisRecord.category == category)
    if status:
        stmt = stmt.where(SeoDiagnosisRecord.status == status)
    rows = db.scalars(stmt.order_by(SeoDiagnosisRecord.priority_score.desc(), SeoDiagnosisRecord.id.desc())).all()
    kanban = {"new": [], "planned": [], "in_progress": [], "completed": [], "ignored": []}
    for r in rows:
        st = r.status if r.status in kanban else "new"
        kanban[st].append(diagnosis._diagnosis_out(r))
    return {
        "site_id": site_id,
        "total": len(rows),
        "items": [diagnosis._diagnosis_out(r) for r in rows],
        "kanban": kanban,
    }


@router.post("/sites/{site_id}/seo-diagnoses/run")
def run_diagnosis(site_id: int, db: Session = Depends(get_db), _: Principal = Depends(current_principal)):
    site = get_or_404(db, Site, site_id, "site")
    return diagnosis.run_seo_diagnosis(db, site)


@router.patch("/seo-diagnoses/{diagnosis_id}")
def update_diagnosis_status(
    diagnosis_id: int,
    body: StatusUpdateIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    rec = get_or_404(db, SeoDiagnosisRecord, diagnosis_id, "seo diagnosis")
    if rec.org_id != p.org_id:
        raise ApiError(404, "not_found", "diagnosis not found")
    rec.status = body.status
    db.commit()
    return diagnosis._diagnosis_out(rec)
