"""Auth, organizations/workspaces/projects, members, invites, API keys, credits, audit log, GDPR, admin."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, Page, get_or_404, paginate
from app.auth import (
    ROLE_RANK,
    SESSION_COOKIE,
    Principal,
    audit,
    current_principal,
    get_db,
    require_platform_admin,
    require_role,
)
from app.core import crypto
from app.core.config import get_settings
from app.core.security import (
    hash_password,
    new_api_key,
    new_token,
    sha256,
    totp_secret,
    totp_uri,
    verify_password,
    verify_totp,
)
from app.db.session import SessionLocal
from app.infra.ratelimit import TokenBucket
from app.models import (
    ApiKey,
    AuditLog,
    AuthSession,
    Invite,
    Membership,
    Organization,
    Project,
    Site,
    User,
    Workspace,
)
from app.services import credits

router = APIRouter()
SESSION_TTL = timedelta(hours=12)
_login_bucket = TokenBucket(prefix="login")


def _now():
    return datetime.now(UTC)


# ---------------------------------------------------------------- auth (no principal required)

class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    totp_code: str | None = Field(None, max_length=10)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    if _login_bucket.try_acquire(f"{ip}:{body.email.lower()}", rate=5 / 60, burst=5) > 0:
        raise ApiError(429, "rate_limited", "too many login attempts; try again later")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
        ok = verify_password(user.password_hash if user else None, body.password)
        if not ok or not user.is_active:
            db.add(AuditLog(actor="anonymous", action="auth.login_failed", target=body.email.lower()[:255], ip=ip))
            db.commit()
            raise ApiError(401, "unauthorized", "invalid credentials")
        if user.totp_enabled:
            if not body.totp_code or not verify_totp(crypto.decrypt(user.totp_secret_encrypted), body.totp_code):
                raise ApiError(401, "totp_required", "valid TOTP code required")
        token, token_hash = new_token()
        csrf, _ = new_token(24)
        db.add(AuthSession(token_hash=token_hash, user_id=user.id, csrf_token=csrf, expires_at=_now() + SESSION_TTL))
        db.add(AuditLog(actor=f"user:{user.id}", action="auth.login", ip=ip))
        db.commit()
    secure = get_settings().app_mode == "SERVER"
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure, samesite="strict",
                        max_age=int(SESSION_TTL.total_seconds()), path="/")
    return {"csrf_token": csrf, "expires_in": int(SESSION_TTL.total_seconds())}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with SessionLocal() as db:
            db.execute(delete(AuthSession).where(AuthSession.token_hash == sha256(token)))
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"logged_out": True}


class AcceptInviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=12, max_length=1024)


@router.post("/auth/invites/accept")
def accept_invite(body: AcceptInviteIn):
    with SessionLocal() as db:
        inv = db.scalar(select(Invite).where(Invite.token_hash == sha256(body.token)))
        if inv is None or inv.accepted_at or inv.expires_at < _now():
            raise ApiError(400, "bad_request", "invalid or expired invite")
        user = db.scalar(select(User).where(func.lower(User.email) == inv.email.lower()))
        if user is None:
            user = User(email=inv.email, password_hash=hash_password(body.password), email_verified=True)
            db.add(user)
            db.flush()
        if not db.scalar(select(Membership).where(Membership.org_id == inv.org_id, Membership.user_id == user.id)):
            db.add(Membership(org_id=inv.org_id, user_id=user.id, role=inv.role))
        inv.accepted_at = _now()
        db.add(AuditLog(org_id=inv.org_id, actor=f"user:{user.id}", action="invite.accept", target=inv.email))
        db.commit()
        return {"user_id": user.id, "org_id": inv.org_id, "role": inv.role}


@router.get("/me")
def me(p: Principal = Depends(current_principal)):
    out = {"org_id": p.org_id, "role": p.role, "actor": p.actor, "via": p.via, "mode": get_settings().app_mode,
           "is_platform_admin": p.is_platform_admin, "user": None}
    if p.user_id:
        with SessionLocal() as db:
            u = db.get(User, p.user_id)
            out["user"] = {"id": u.id, "email": u.email, "totp_enabled": u.totp_enabled, "email_verified": u.email_verified}
    return out


@router.post("/me/totp/setup")
def totp_setup(p: Principal = Depends(current_principal)):
    if not p.user_id:
        raise ApiError(400, "bad_request", "TOTP requires a user session")
    with SessionLocal() as db:
        u = db.get(User, p.user_id)
        secret = totp_secret()
        u.totp_secret_encrypted, u.totp_enabled = crypto.encrypt(secret), False
        db.commit()
        return {"secret": secret, "otpauth_uri": totp_uri(secret, u.email)}


class TotpIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


@router.post("/me/totp/enable")
def totp_enable(body: TotpIn, p: Principal = Depends(current_principal)):
    with SessionLocal() as db:
        u = db.get(User, p.user_id) if p.user_id else None
        if u is None or not u.totp_secret_encrypted or not verify_totp(crypto.decrypt(u.totp_secret_encrypted), body.code):
            raise ApiError(400, "bad_request", "invalid TOTP code")
        u.totp_enabled = True
        db.add(AuditLog(org_id=p.org_id, actor=p.actor, action="auth.totp_enabled"))
        db.commit()
        return {"totp_enabled": True}


# ---------------------------------------------------------------- org structure

class NameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    workspace_id: int | None = None


@router.get("/workspaces")
def list_workspaces(page: Page = Depends(), db: Session = Depends(get_db)):
    return paginate(db, select(Workspace).order_by(Workspace.id), page,
                    lambda w: {"id": w.id, "org_id": w.org_id, "name": w.name})


@router.post("/workspaces", status_code=201)
def create_workspace(body: NameIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    w = Workspace(org_id=p.org_id, name=body.name)
    db.add(w)
    db.flush()
    audit(db, "workspace.create", f"workspace:{w.id}")
    db.commit()
    return {"id": w.id, "org_id": w.org_id, "name": w.name}


@router.get("/projects")
def list_projects(page: Page = Depends(), db: Session = Depends(get_db)):
    return paginate(db, select(Project).order_by(Project.id), page,
                    lambda x: {"id": x.id, "org_id": x.org_id, "workspace_id": x.workspace_id, "name": x.name})


@router.post("/projects", status_code=201)
def create_project(body: NameIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("editor"))):
    try:
        credits.check_limit(db, p.org_id, "projects")
    except credits.PlanLimitExceeded as exc:
        raise ApiError(402, "plan_limit", str(exc)) from exc
    ws = db.get(Workspace, body.workspace_id) if body.workspace_id else db.scalar(
        select(Workspace).where(Workspace.org_id == p.org_id).order_by(Workspace.id).limit(1))
    if ws is None:
        raise ApiError(422, "validation_error", "workspace not found")
    x = Project(org_id=p.org_id, workspace_id=ws.id, name=body.name)
    db.add(x)
    db.flush()
    audit(db, "project.create", f"project:{x.id}")
    db.commit()
    return {"id": x.id, "org_id": x.org_id, "workspace_id": x.workspace_id, "name": x.name}


# ---------------------------------------------------------------- members & invites

class InviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role: str = Field(pattern="^(admin|editor|viewer|client)$")


@router.get("/members")
def members(db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    rows = db.execute(select(Membership, User.email).join(User, User.id == Membership.user_id)
                      .where(Membership.org_id == p.org_id).order_by(Membership.id)).all()
    return {"items": [{"user_id": m.user_id, "email": e, "role": m.role} for m, e in rows]}


@router.post("/invites", status_code=201)
def invite(body: InviteIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    try:
        credits.check_limit(db, p.org_id, "users")
    except credits.PlanLimitExceeded as exc:
        raise ApiError(402, "plan_limit", str(exc)) from exc
    token, token_hash = new_token()
    inv = Invite(org_id=p.org_id, email=body.email, role=body.role, token_hash=token_hash,
                 expires_at=_now() + timedelta(days=7))
    db.add(inv)
    audit(db, "invite.create", body.email, {"role": body.role})
    db.commit()
    # No SMTP configured → token returned once to the inviting admin to deliver out-of-band.
    return {"id": inv.id, "email": inv.email, "role": inv.role, "token": token,
            "delivery": "email not configured; share this token securely (shown once)"}


class RoleIn(BaseModel):
    role: str = Field(pattern="^(owner|admin|editor|viewer|client)$")


@router.patch("/members/{user_id}")
def change_role(user_id: int, body: RoleIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    m = db.scalar(select(Membership).where(Membership.org_id == p.org_id, Membership.user_id == user_id))
    if m is None:
        raise ApiError(404, "not_found", "member not found")
    if (body.role == "owner" or m.role == "owner") and not p.at_least("owner"):
        raise ApiError(403, "forbidden", "only owners can grant or revoke owner")
    audit(db, "member.role_change", f"user:{user_id}", {"from": m.role, "to": body.role})
    m.role = body.role
    db.commit()
    return {"user_id": user_id, "role": m.role}


# ---------------------------------------------------------------- API keys

class ApiKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    role: str = Field("viewer", pattern="^(admin|editor|viewer|client)$")
    ip_allowlist: list[str] = Field(default_factory=list, max_length=50)


def _key_out(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "role": k.role, "ip_allowlist": k.ip_allowlist,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None}


@router.post("/api-keys", status_code=201)
def create_key(body: ApiKeyIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    if ROLE_RANK[body.role] > ROLE_RANK[p.role]:
        raise ApiError(403, "forbidden", "cannot create a key with a higher role than your own")
    full, prefix, key_hash = new_api_key()
    k = ApiKey(org_id=p.org_id, name=body.name, prefix=prefix, key_hash=key_hash, role=body.role,
               ip_allowlist=body.ip_allowlist, created_by=p.user_id)
    db.add(k)
    db.flush()
    audit(db, "api_key.create", f"apikey:{prefix}", {"role": body.role})
    db.commit()
    return {**_key_out(k), "key": full, "note": "Store this key now; it will not be shown again."}


@router.get("/api-keys")
def list_keys(db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    return {"items": [_key_out(k) for k in db.scalars(select(ApiKey).where(ApiKey.org_id == p.org_id).order_by(ApiKey.id))]}


@router.delete("/api-keys/{key_id}")
def revoke_key(key_id: int, db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    k = get_or_404(db, ApiKey, key_id, "api key")
    k.revoked_at = _now()
    audit(db, "api_key.revoke", f"apikey:{k.prefix}")
    db.commit()
    return _key_out(k)


# ---------------------------------------------------------------- credits & audit

@router.get("/credits/usage")
def credit_usage(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    return credits.usage(db, p.org_id)


@router.get("/credits/preflight")
def credit_preflight(operation: str, quantity: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    try:
        return credits.preflight(db, p.org_id, operation, quantity)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc


@router.get("/audit-log")
def audit_log(page: Page = Depends(), db: Session = Depends(get_db), p: Principal = Depends(require_role("admin"))):
    stmt = select(AuditLog).where(AuditLog.org_id == p.org_id).order_by(AuditLog.id.desc())
    return paginate(db, stmt, page, lambda a: {"id": a.id, "actor": a.actor, "action": a.action, "target": a.target,
                                                "details": a.details, "created_at": a.created_at.isoformat()})


# ---------------------------------------------------------------- GDPR / KVKK

@router.get("/org/export")
def export_org(db: Session = Depends(get_db), p: Principal = Depends(require_role("owner"))):
    """Tenant data export (control-plane data). Credentials are never exported."""
    from app.models import Competitor, GrowthOpportunity
    org = db.get(Organization, p.org_id)
    sites = db.scalars(select(Site)).all()  # RLS-scoped
    audit(db, "org.export", f"org:{p.org_id}")
    db.commit()
    return {"organization": {"id": org.id, "name": org.name, "plan": org.plan},
            "projects": [{"id": x.id, "name": x.name} for x in db.scalars(select(Project))],
            "sites": [{"id": s.id, "name": s.name, "base_url": s.base_url, "gsc_property": s.gsc_property,
                       "ga4_property_id": s.ga4_property_id} for s in sites],
            "competitors": [{"site_id": c.site_id, "name": c.name, "domain": c.domain} for c in db.scalars(select(Competitor))],
            "opportunities": [{"id": o.id, "site_id": o.site_id, "type": o.opportunity_type, "status": o.status,
                               "title": o.title} for o in db.scalars(select(GrowthOpportunity))],
            "note": "GSC/GA4 fact tables available via the data endpoints; credentials are excluded."}


class DeleteOrgIn(BaseModel):
    confirm_name: str


@router.post("/org/delete")
def delete_org(body: DeleteOrgIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("owner"))):
    org = db.get(Organization, p.org_id)
    if body.confirm_name != org.name:
        raise ApiError(422, "validation_error", "confirm_name must equal the organization name")
    if org.id == 1 and get_settings().app_mode == "LOCAL_SINGLE_USER":
        raise ApiError(409, "conflict", "the local organization cannot be deleted in LOCAL mode")
    with SessionLocal() as sysdb:  # cascade delete of tenant data only; global datasets untouched
        sysdb.add(AuditLog(org_id=None, actor=p.actor, action="org.delete", target=f"org:{org.id}"))
        sysdb.execute(delete(Organization).where(Organization.id == org.id))
        sysdb.commit()
    return {"deleted": True}


# ---------------------------------------------------------------- platform admin

@router.get("/admin/tenants")
def admin_tenants(p: Principal = Depends(require_platform_admin)):
    with SessionLocal() as db:  # system context: all tenants
        out = []
        for org in db.scalars(select(Organization).order_by(Organization.id)):
            out.append({"id": org.id, "name": org.name, "plan": org.plan, "suspended": org.suspended, "is_demo": org.is_demo,
                        "sites": db.scalar(select(func.count()).select_from(Site).where(Site.org_id == org.id)),
                        "usage": credits.usage(db, org.id)})
        return {"items": out}


class SuspendIn(BaseModel):
    suspended: bool


@router.patch("/admin/tenants/{org_id}")
def admin_suspend(org_id: int, body: SuspendIn, p: Principal = Depends(require_platform_admin)):
    with SessionLocal() as db:
        org = db.get(Organization, org_id)
        if org is None:
            raise ApiError(404, "not_found", "organization not found")
        org.suspended = body.suspended
        db.add(AuditLog(org_id=org_id, actor=p.actor, action="admin.suspend" if body.suspended else "admin.unsuspend"))
        db.commit()
        return {"id": org.id, "suspended": org.suspended}


PLAN_PRICE_USD = {"local": 0.0, "starter": 49.0, "pro": 149.0, "agency": 399.0}


@router.get("/admin/margin")
def admin_margin(p: Principal = Depends(require_platform_admin)):
    """Admin cost vs revenue margin dashboard per tenant and provider (§2)."""
    from app.models import ProviderUsage
    with SessionLocal() as db:
        tenants = []
        total_rev = total_cost = 0.0
        for org in db.scalars(select(Organization).order_by(Organization.id)):
            u = credits.usage(db, org.id)
            rev = PLAN_PRICE_USD.get(org.plan, 0.0)
            cost = float(u.get("provider_cost_usd_this_month") or 0.0)
            total_rev += rev
            total_cost += cost
            tenants.append({
                "org_id": org.id,
                "name": org.name,
                "plan": org.plan,
                "plan_revenue_usd": rev,
                "provider_cost_usd": round(cost, 4),
                "margin_usd": round(rev - cost, 4),
                "margin_pct": round((rev - cost) / rev * 100, 1) if rev > 0 else None,
                "credits_used": u.get("used_this_month", 0),
            })
        by_provider = [
            {"provider": prov, "calls": int(cnt), "cost_usd": round(float(c or 0.0), 4)}
            for prov, cnt, c in db.execute(
                select(ProviderUsage.provider, func.count(), func.sum(ProviderUsage.cost_usd))
                .group_by(ProviderUsage.provider)
            ).all()
        ]
        return {
            "total_plan_revenue_usd": round(total_rev, 2),
            "total_provider_cost_usd": round(total_cost, 4),
            "total_margin_usd": round(total_rev - total_cost, 4),
            "tenants": tenants,
            "by_provider": by_provider,
        }


class OrgSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cross_tenant_opt_in: bool | None = None
    is_demo: bool | None = None


@router.get("/org/settings")
def get_org_settings(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    org = db.get(Organization, p.org_id)
    return {
        "id": org.id if org else p.org_id,
        "name": org.name if org else "Local",
        "is_demo": bool(org.is_demo) if org else False,
        "cross_tenant_opt_in": bool(org.cross_tenant_opt_in) if org else False,
    }


@router.patch("/org/settings")
def update_org_settings(body: OrgSettingsIn, db: Session = Depends(get_db), p: Principal = Depends(require_role("owner"))):
    org = db.get(Organization, p.org_id)
    if body.cross_tenant_opt_in is not None:
        org.cross_tenant_opt_in = body.cross_tenant_opt_in
        audit(db, "org.settings", f"org:{org.id}", {"cross_tenant_opt_in": body.cross_tenant_opt_in})
    if body.is_demo is not None:
        org.is_demo = body.is_demo
        audit(db, "org.settings", f"org:{org.id}", {"is_demo": body.is_demo})
    db.commit()
    return {"id": org.id, "cross_tenant_opt_in": org.cross_tenant_opt_in, "is_demo": org.is_demo}


@router.get("/billing/status")
def billing_status(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    s = get_settings()
    org = db.get(Organization, p.org_id)
    stripe_enabled = bool(s.stripe_secret_key and s.stripe_secret_key.startswith("sk_test_"))
    return {
        "plan": org.plan if org else "local",
        "stripe_enabled": stripe_enabled,
        "test_mode_only": True,
        "oidc_sso_enabled": bool(s.oidc_issuer_url and s.oidc_client_id),
        "note": "Stripe billing is feature-flagged and restricted to test keys (sk_test_*) locally.",
    }


def bootstrap_org(db: Session, name: str, owner_email: str, password: str, plan: str = "starter",
                  platform_admin: bool = False) -> tuple[Organization, User]:
    """Create org + default workspace/project + owner user (CLI `seo org-create`)."""
    org = Organization(name=name, plan=plan, is_demo=False, suspended=False, cross_tenant_opt_in=False)
    db.add(org)
    db.flush()
    ws = Workspace(org_id=org.id, name="Default")
    db.add(ws)
    db.flush()
    db.add(Project(org_id=org.id, workspace_id=ws.id, name="Default"))
    user = db.scalar(select(User).where(func.lower(User.email) == owner_email.lower()))
    if user is None:
        user = User(email=owner_email, password_hash=hash_password(password), email_verified=True,
                    is_platform_admin=platform_admin, is_active=True, totp_enabled=False)
        db.add(user)
        db.flush()
    db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
    db.add(AuditLog(org_id=org.id, actor="system", action="org.create", target=f"org:{org.id}"))
    db.commit()
    return org, user

