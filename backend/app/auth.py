"""Principal resolution, RBAC, tenant-scoped DB sessions (RLS), audit log.

Modes:
- LOCAL_SINGLE_USER: requests from loopback without credentials act as the local owner of org 1.
  Non-loopback clients are rejected (the app must not be exposed without auth).
- SERVER: every request needs a session cookie (+CSRF header on unsafe methods) or an API key.
"""

import ipaddress
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import parse_api_key, sha256
from app.db.session import SessionLocal
from app.models import ApiKey, AuditLog, AuthSession, Membership, Organization, User

ROLE_RANK = {"client": 0, "viewer": 1, "editor": 2, "admin": 3, "owner": 4}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SESSION_COOKIE = "sgi_session"
LOCAL_ORG_ID = 1


@dataclass
class Principal:
    org_id: int
    role: str
    actor: str
    user_id: int | None = None
    via: str = "local"  # local | session | api_key
    is_platform_admin: bool = False

    def at_least(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "details": None})


def _is_loopback(host: str | None) -> bool:
    if host in ("testclient", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def resolve_principal(request: Request) -> Principal:
    s = get_settings()
    client_ip = request.client.host if request.client else None
    auth = request.headers.get("authorization", "")
    with SessionLocal() as db:
        if auth.lower().startswith("bearer "):
            full = auth[7:].strip()
            prefix = parse_api_key(full)
            key = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix)) if prefix else None
            if key is None or key.revoked_at is not None or key.key_hash != sha256(full):
                raise _err(401, "unauthorized", "invalid API key")
            if key.ip_allowlist and client_ip not in key.ip_allowlist:
                raise _err(403, "forbidden", "client IP not allowed for this key")
            key.last_used_at = datetime.now(UTC)
            db.commit()
            _api_key_rate_limit(db, key)
            p = Principal(org_id=key.org_id, role=key.role, actor=f"apikey:{key.prefix}", via="api_key")
            return _check_org(db, p)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            sess = db.get(AuthSession, sha256(token))
            if sess is None or sess.expires_at < datetime.now(UTC):
                raise _err(401, "unauthorized", "session expired")
            if request.method not in SAFE_METHODS and request.headers.get("x-csrf-token") != sess.csrf_token:
                raise _err(403, "csrf_failed", "missing or invalid CSRF token")
            user = db.get(User, sess.user_id)
            if user is None or not user.is_active:
                raise _err(401, "unauthorized", "user inactive")
            wanted = request.headers.get("x-org-id")
            q = select(Membership).where(Membership.user_id == user.id)
            if wanted and wanted.isdigit():
                q = q.where(Membership.org_id == int(wanted))
            m = db.scalar(q.order_by(Membership.org_id).limit(1))
            if m is None:
                raise _err(403, "forbidden", "no membership in this organization")
            p = Principal(org_id=m.org_id, role=m.role, actor=f"user:{user.id}", user_id=user.id, via="session",
                          is_platform_admin=user.is_platform_admin)
            return _check_org(db, p)
    if s.app_mode == "LOCAL_SINGLE_USER":
        if not _is_loopback(client_ip):
            raise _err(403, "forbidden", "LOCAL_SINGLE_USER mode only accepts loopback clients")
        return Principal(org_id=LOCAL_ORG_ID, role="owner", actor="local", via="local", is_platform_admin=True)
    raise _err(401, "unauthorized", "authentication required")


def _check_org(db: Session, p: Principal) -> Principal:
    org = db.get(Organization, p.org_id)
    if org is None or org.suspended:
        raise _err(403, "forbidden", "organization suspended or missing")
    return p


def current_principal(request: Request) -> Principal:
    p = resolve_principal(request)
    needed = "viewer" if request.method in SAFE_METHODS else "editor"
    if not p.at_least(needed):
        raise _err(403, "forbidden", f"role {p.role!r} cannot perform {request.method}")
    request.state.principal = p
    return p


def require_role(role: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.at_least(role):
            raise _err(403, "forbidden", f"requires role {role}")
        return p
    return dep


def require_platform_admin(p: Principal = Depends(current_principal)) -> Principal:
    if not p.is_platform_admin:
        raise _err(403, "forbidden", "platform admin only")
    return p


def scoped_session(org_id: int | None) -> Session:
    """Session whose queries are filtered by Postgres RLS to one organization."""
    db = SessionLocal()
    db.execute(text("SELECT set_config('app.org_id', :v, false)"), {"v": str(org_id) if org_id else ""})
    return db


def get_db(p: Principal = Depends(current_principal)) -> Iterator[Session]:
    db = scoped_session(p.org_id)
    db.info["principal"] = p
    try:
        yield db
    finally:
        try:
            db.rollback()
            db.execute(text("RESET app.org_id"))
            db.commit()
        finally:
            db.close()


def audit(db: Session, action: str, target: str | None = None, details: dict | None = None,
          principal: Principal | None = None, ip: str | None = None) -> None:
    p = principal or db.info.get("principal")
    db.add(AuditLog(org_id=p.org_id if p else None, actor=p.actor if p else "system", action=action,
                    target=target, details=details or {}, ip=ip))


API_RATE = {"starter": (2.0, 10.0), "pro": (10.0, 50.0)}  # (requests/s, burst) per API key; local plan unlimited
_rate_bucket = None


def _api_key_rate_limit(db: Session, key: ApiKey) -> None:
    global _rate_bucket
    org = db.get(Organization, key.org_id)
    limits = API_RATE.get(org.plan) if org else None
    if not limits:
        return
    if _rate_bucket is None:
        from app.infra.ratelimit import TokenBucket
        _rate_bucket = TokenBucket(prefix="apikey")
    wait = _rate_bucket.try_acquire(key.prefix, rate=limits[0], burst=limits[1])
    if wait > 0:
        raise HTTPException(status_code=429, detail={"code": "rate_limited", "message": f"API rate limit for plan {org.plan}",
                                                     "details": {"retry_after_s": round(wait, 2)}}, headers={"Retry-After": str(max(1, round(wait)))})
