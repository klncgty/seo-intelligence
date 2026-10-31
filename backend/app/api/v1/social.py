"""Module 10: Social Media Management API."""

from datetime import datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, get_or_404
from app.auth import Principal, current_principal, get_db
from app.models import Site, SocialAccount, SocialPost
from app.services import social

router = APIRouter()


class GenerateDraftsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str = Field(min_length=5, max_length=2048)
    title: str = Field(min_length=3, max_length=300)
    topic: str = Field(min_length=2, max_length=300)
    platforms: list[str] = Field(default_factory=lambda: ["x", "linkedin"])


class ApprovePostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_at: datetime | None = None


class ConnectAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str = Field(pattern="^(x|linkedin|instagram|facebook|youtube)$")
    handle: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)


@router.get("/sites/{site_id}/social/accounts")
def list_accounts(site_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    get_or_404(db, Site, site_id, "site")
    return {"items": social.list_accounts(db, p.org_id, site_id)}


@router.post("/sites/{site_id}/social/accounts", status_code=201)
def connect_account(site_id: int, body: ConnectAccountIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    site = get_or_404(db, Site, site_id, "site")
    acc = SocialAccount(
        org_id=p.org_id,
        site_id=site.id,
        platform=body.platform,
        handle=body.handle,
        display_name=body.display_name,
        status="credentials_missing",
    )
    db.add(acc)
    db.commit()
    return social._account_out(acc)


@router.get("/sites/{site_id}/social/posts")
def list_posts(
    site_id: int,
    status: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    get_or_404(db, Site, site_id, "site")
    stmt = select(SocialPost).where(SocialPost.org_id == p.org_id, SocialPost.site_id == site_id)
    if status:
        stmt = stmt.where(SocialPost.status == status)
    rows = db.scalars(stmt.order_by(SocialPost.id.desc())).all()
    return {"items": [social._post_out(post) for post in rows]}


@router.post("/sites/{site_id}/social/generate-drafts")
def generate_drafts(site_id: int, body: GenerateDraftsIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    site = get_or_404(db, Site, site_id, "site")
    posts = social.generate_social_drafts_from_content(
        db, site, p.org_id, body.source_url, body.title, body.topic, body.platforms
    )
    return {"items": posts}


@router.post("/social/posts/{post_id}/approve")
def approve_post(post_id: int, body: ApprovePostIn | None = None, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    try:
        return social.approve_and_schedule_post(db, post_id, p.org_id, body.scheduled_at if body else None)
    except ValueError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc


@router.post("/social/posts/{post_id}/publish")
def publish_post(post_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    try:
        return social.publish_post(db, post_id, p.org_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
