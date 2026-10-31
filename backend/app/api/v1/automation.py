"""Automation API: channels, alert rules, digests/reports, exports, share links, tickets (spec §17)."""

import json
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.api.v1.common import ApiError, Page, get_or_404, paginate
from app.auth import Principal, audit, current_principal, get_db, require_role
from app.core import crypto
from app.models import AlertEvent, AlertRule, GrowthOpportunity, NotificationChannel, Organization, ShareLink, Site, TicketLink
from app.services import automation as auto
from app.services import credits
from app.services.keywords import service as kw_service

router = APIRouter()


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(webhook|slack|teams|email)$")
    name: str = Field(min_length=1, max_length=200)
    config: dict


def _ch_out(c: NotificationChannel, secret: str | None = None) -> dict:
    out = {"id": c.id, "kind": c.kind, "name": c.name, "enabled": c.enabled}
    if secret:
        out["signing_secret"] = secret
        out["note"] = "Store the signing secret now; it will not be shown again."
    return out


@router.post("/channels", status_code=201)
def create_channel(body: ChannelIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    try:
        cfg = auto.validate_channel(body.kind, body.config)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    c = NotificationChannel(org_id=p.org_id, kind=body.kind, name=body.name, config_encrypted=crypto.encrypt(json.dumps(cfg)))
    db.add(c)
    audit(db, "channel.create", None, {"kind": body.kind})
    db.commit()
    return _ch_out(c, cfg.get("secret"))


@router.get("/channels")
def list_channels(db: Session = Depends(get_db)):
    return {"items": [_ch_out(c) for c in db.scalars(select(NotificationChannel).order_by(NotificationChannel.id))]}


@router.post("/channels/{cid}/test")
async def test_channel(cid: int, db: Session = Depends(get_db)):
    return await auto.deliver(get_or_404(db, NotificationChannel, cid, "channel"), "Test notification", {"ok": True})


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    rule_type: str = Field(pattern="^(metric_change|anomaly|rank_alerts|crawl_regression)$")
    site_id: int | None = None
    config: dict = Field(default_factory=dict)
    severity: str = Field("medium", pattern="^(critical|high|medium|low|info)$")
    min_volume: int = Field(0, ge=0)
    cooldown_hours: int = Field(24, ge=0, le=24 * 30)
    channel_ids: list[int] = Field(default_factory=list, max_length=10)


def _rule_out(r: AlertRule) -> dict:
    return {"id": r.id, "name": r.name, "rule_type": r.rule_type, "site_id": r.site_id, "config": r.config, "severity": r.severity,
            "min_volume": r.min_volume, "cooldown_hours": r.cooldown_hours, "channel_ids": r.channel_ids, "enabled": r.enabled,
            "snoozed_until": r.snoozed_until.isoformat() if r.snoozed_until else None,
            "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None}


@router.post("/alert-rules", status_code=201)
def create_rule(body: RuleIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    if body.site_id:
        get_or_404(db, Site, body.site_id, "site")
    for cid in body.channel_ids:
        get_or_404(db, NotificationChannel, cid, "channel")
    r = AlertRule(org_id=p.org_id, **body.model_dump())
    db.add(r)
    jobs.upsert_schedule(db, "alerts:evaluate", "alerts_evaluate", 3600)
    db.commit()
    return _rule_out(r)


@router.get("/alert-rules")
def list_rules(db: Session = Depends(get_db)):
    return {"items": [_rule_out(r) for r in db.scalars(select(AlertRule).order_by(AlertRule.id))]}


class SnoozeIn(BaseModel):
    hours: int = Field(ge=0, le=24 * 90)


@router.post("/alert-rules/{rid}/snooze")
def snooze(rid: int, body: SnoozeIn, db: Session = Depends(get_db)):
    from datetime import UTC, datetime, timedelta
    r = get_or_404(db, AlertRule, rid, "alert rule")
    r.snoozed_until = datetime.now(UTC) + timedelta(hours=body.hours) if body.hours else None
    db.commit()
    return _rule_out(r)


@router.post("/alerts/evaluate")
async def evaluate(db: Session = Depends(get_db)):
    return await auto.evaluate_rules(db)  # RLS-scoped: only this organization's rules


@router.get("/alert-events")
def events(page: Page = Depends(), db: Session = Depends(get_db)):
    return paginate(db, select(AlertEvent).order_by(AlertEvent.id.desc()), page,
                    lambda e: {"id": e.id, "rule_id": e.rule_id, "severity": e.severity, "payload": e.payload,
                               "deliveries": e.deliveries, "created_at": e.created_at.isoformat()})


@router.get("/sites/{site_id}/digest")
def digest(site_id: int, db: Session = Depends(get_db)):
    return auto.weekly_digest(db, get_or_404(db, Site, site_id, "site"))


@router.get("/sites/{site_id}/report.html", response_class=HTMLResponse)
def report_html(site_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    site = get_or_404(db, Site, site_id, "site")
    org = db.get(Organization, p.org_id)
    return HTMLResponse(auto.render_report_html(auto.weekly_digest(db, site), org.branding),
                        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


class DigestScheduleIn(BaseModel):
    channel_ids: list[int] = Field(min_length=1, max_length=10)


@router.post("/sites/{site_id}/digest/schedule")
def schedule_digest(site_id: int, body: DigestScheduleIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    get_or_404(db, Site, site_id, "site")
    for cid in body.channel_ids:
        get_or_404(db, NotificationChannel, cid, "channel")
    jobs.upsert_schedule(db, f"digest:site:{site_id}", "weekly_digest", 7 * 86400, {"site_id": site_id, "channel_ids": body.channel_ids},
                         tenant_id=p.org_id)
    return {"scheduled": True, "interval": "weekly"}


class BrandingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=100)
    color: str | None = Field(None, pattern="^#[0-9a-fA-F]{6}$")


@router.patch("/org/branding")
def branding(body: BrandingIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    org = db.get(Organization, p.org_id)
    org.branding = {**(org.branding or {}), **body.model_dump(exclude_none=True)}
    db.commit()
    return org.branding


@router.get("/exports/{kind}")
def export(kind: str, fmt: str = Query("csv", pattern="^(csv|xlsx)$"), site_id: int | None = None, confirm: bool = False,
           db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    try:
        rows = auto.export_rows(db, kind, site_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    try:
        kw_service._charge_or_preflight(db, p.org_id, "export_row", len(rows), confirm, f"export:{kind}:{uuid.uuid4().hex}")
    except kw_service.ConfirmationRequired as exc:
        raise ApiError(428, "confirmation_required", "re-send with confirm=true to accept the credit estimate", exc.preflight) from exc
    except credits.InsufficientCredits as exc:
        raise ApiError(402, "insufficient_credits", str(exc)) from exc
    if fmt == "csv":
        return Response(auto.to_csv(rows), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'})
    return Response(auto.to_xlsx(rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{kind}.xlsx"'})


class ShareIn(BaseModel):
    ttl_hours: int = Field(168, ge=1, le=24 * 90)


@router.post("/sites/{site_id}/share", status_code=201)
def share(site_id: int, body: ShareIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    get_or_404(db, Site, site_id, "site")
    token, link = auto.create_share(db, p.org_id, f"report:site:{site_id}", body.ttl_hours)
    return {"id": link.id, "url_path": f"/share/{token}", "expires_at": link.expires_at.isoformat(), "note": "Token shown once."}


@router.delete("/share-links/{lid}")
def revoke_share(lid: int, db: Session = Depends(get_db)):
    from datetime import UTC, datetime
    link = get_or_404(db, ShareLink, lid, "share link")
    link.revoked_at = datetime.now(UTC)
    db.commit()
    return {"revoked": True}


class TicketIn(BaseModel):
    tracker: str = Field(pattern="^(github|jira|linear)$")


@router.post("/opportunities/{opp_id}/ticket", status_code=201)
async def ticket(opp_id: int, body: TicketIn, db: Session = Depends(get_db)):
    o = get_or_404(db, GrowthOpportunity, opp_id, "opportunity")
    try:
        link = await auto.create_ticket(db, o, body.tracker)
    except ValueError as exc:
        raise ApiError(400, "integration_not_configured", str(exc)) from exc
    return {"tracker": link.tracker, "external_id": link.external_id, "url": link.url, "opportunity_status": o.status}


@router.post("/tickets/{tid}/sync")
async def sync_ticket(tid: int, db: Session = Depends(get_db)):
    link = get_or_404(db, TicketLink, tid, "ticket")
    if link.tracker != "github":
        raise ApiError(400, "not_supported", "status sync implemented for GitHub only")
    link = await auto.sync_github(db, link)
    return {"state": link.state, "opportunity_status": db.get(GrowthOpportunity, link.opportunity_id).status}


class AsyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(opportunities_regenerate|domain_strength|backlink_verify)$")
    params: dict = Field(default_factory=dict)
    callback_url: str | None = Field(None, max_length=2048)


@router.post("/async-jobs", status_code=202)
def async_job(body: AsyncIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    """Heavy operations as jobs; optional webhook callback (SSRF policy + HMAC signature with the org's first webhook secret)."""
    if body.callback_url:
        from app.schemas import _validate_base_url
        try:
            _validate_base_url(body.callback_url)
        except ValueError as exc:
            raise ApiError(422, "validation_error", str(exc)) from exc
    if body.kind == "opportunities_regenerate":
        get_or_404(db, Site, int(body.params.get("site_id", 0)), "site")
    j = jobs.enqueue(db, "async_wrapper", {"kind": body.kind, "params": body.params, "callback_url": body.callback_url,
                                           "org_id": p.org_id}, tenant_id=p.org_id)
    return {"job_id": j.id, "status_url": f"/api/v1/jobs/{j.id}", "events_url": f"/api/v1/jobs/{j.id}/events"}
