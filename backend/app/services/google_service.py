"""Google OAuth state/token handling and idempotent GSC/GA4 synchronisation."""

import logging
import secrets
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import get_settings
from app.models import (
    IntegrationCredential,
    OAuthState,
    OrganicLandingDaily,
    SearchPerformanceDaily,
    SearchTotalsDaily,
    Site,
    SyncJob,
)
from app.services.integrations import analytics, google_oauth, search_console

log = logging.getLogger(__name__)
PROVIDER = "google"
STATE_TTL = timedelta(minutes=10)
# GSC data for the last ~2-3 days is not final; resync a trailing window on every incremental run.
GSC_RESYNC_DAYS = 4
GA4_RESYNC_DAYS = 3
UPSERT_BATCH = 1000

SETUP_INSTRUCTIONS = (
    "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI and TOKEN_ENCRYPTION_KEY in .env, "
    "restart the API, then run `seo google connect` and open the printed URL. See README → Google Cloud OAuth setup."
)


class IntegrationNotConfigured(Exception):
    pass


def oauth_configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret and crypto.encryption_configured())


def _org(db: Session, org_id: int | None) -> int:
    if org_id is not None:
        return org_id
    p = db.info.get("principal")
    if p is None:
        raise ValueError("org_id required outside a request context")
    return p.org_id


def get_credential(db: Session, org_id: int | None = None) -> IntegrationCredential | None:
    """Credentials are per organization; never shared across tenants."""
    return db.scalar(select(IntegrationCredential).where(IntegrationCredential.provider == PROVIDER,
                                                         IntegrationCredential.org_id == _org(db, org_id)))


def status(db: Session) -> dict:
    cred = get_credential(db)
    return {
        "oauth_configured": oauth_configured(),
        "connected": cred is not None,
        "scopes": list(cred.scopes) if cred else [],
        "connected_at": cred.updated_at.isoformat() if cred else None,
        "setup_instructions": SETUP_INSTRUCTIONS,
    }


def start_authorization(db: Session) -> str:
    if not oauth_configured():
        raise IntegrationNotConfigured(SETUP_INSTRUCTIONS)
    s = get_settings()
    db.execute(delete(OAuthState).where(OAuthState.created_at < datetime.now(UTC) - STATE_TTL))
    state = secrets.token_urlsafe(32)
    verifier, challenge = google_oauth.generate_pkce_pair()
    db.add(OAuthState(state=state, org_id=_org(db, None), code_verifier_encrypted=crypto.encrypt(verifier)))
    db.commit()
    return google_oauth.build_authorization_url(s.google_client_id, s.google_redirect_uri, state, challenge)


def consume_state(db: Session, state: str) -> tuple[str, int]:
    """Validate single-use, unexpired state (CSRF protection); returns (PKCE verifier, org_id)."""
    row = db.get(OAuthState, state, with_for_update=True) if state else None
    if row is None or row.used or row.created_at < datetime.now(UTC) - STATE_TTL:
        raise ValueError("invalid or expired OAuth state")
    row.used = True
    db.commit()
    return crypto.decrypt(row.code_verifier_encrypted), row.org_id


async def complete_authorization(db: Session, code: str, state: str, client: httpx.AsyncClient | None = None) -> None:
    verifier, org_id = consume_state(db, state)
    s = get_settings()
    async with _client(client) as c:
        tok = await google_oauth.exchange_code(c, s.google_client_id, s.google_client_secret,
                                               s.google_redirect_uri, code, verifier)
    if not tok.refresh_token:
        raise ValueError("Google did not return a refresh token; revoke app access and reconnect")
    cred = get_credential(db, org_id) or IntegrationCredential(provider=PROVIDER, org_id=org_id)
    cred.encrypted_refresh_token = crypto.encrypt(tok.refresh_token)
    cred.scopes = sorted(tok.scope.split())
    db.add(cred)
    db.commit()


def disconnect(db: Session) -> None:
    db.execute(delete(IntegrationCredential).where(IntegrationCredential.provider == PROVIDER,
                                                   IntegrationCredential.org_id == _org(db, None)))
    db.commit()


class _client:
    """Use injected client (tests) or create a short-lived one."""

    def __init__(self, client: httpx.AsyncClient | None):
        self.client, self.own = client, client is None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self.own:
            self.client = httpx.AsyncClient(timeout=60, trust_env=False)
        return self.client

    async def __aexit__(self, *exc):
        if self.own:
            await self.client.aclose()


async def access_token(db: Session, client: httpx.AsyncClient, org_id: int | None = None) -> str:
    cred = get_credential(db, org_id)
    if cred is None or not oauth_configured():
        raise IntegrationNotConfigured("Google account not connected. " + SETUP_INSTRUCTIONS)
    s = get_settings()
    tok = await google_oauth.refresh_access_token(client, s.google_client_id, s.google_client_secret,
                                                  crypto.decrypt(cred.encrypted_refresh_token))
    return tok.access_token


async def list_properties(db: Session, client: httpx.AsyncClient | None = None) -> dict:
    async with _client(client) as c:
        token = await access_token(db, c)
        return {"search_console": await search_console.list_sites(c, token),
                "analytics": await analytics.list_properties(c, token)}


# ---------------------------------------------------------------- sync jobs

def enqueue_sync(db: Session, site: Site, source: str) -> SyncJob:
    if source == "search_console" and not site.gsc_property:
        raise IntegrationNotConfigured("Site has no Search Console property mapped (gsc_property).")
    if source == "analytics" and not site.ga4_property_id:
        raise IntegrationNotConfigured("Site has no GA4 property configured (ga4_property_id).")
    if get_credential(db, site.org_id) is None or not oauth_configured():
        raise IntegrationNotConfigured("Google account not connected. " + SETUP_INSTRUCTIONS)
    existing = db.scalar(select(SyncJob).where(SyncJob.site_id == site.id, SyncJob.source == source,
                                               SyncJob.status.in_(("queued", "running"))))
    if existing:
        return existing
    job = SyncJob(site_id=site.id, source=source)
    db.add(job)
    db.commit()
    return job


def claim_next_sync(db: Session, worker_id: str) -> SyncJob | None:
    job = db.execute(select(SyncJob).where(SyncJob.status == "queued").order_by(SyncJob.id)
                     .limit(1).with_for_update(skip_locked=True)).scalar_one_or_none()
    if job is None:
        db.rollback()
        return None
    job.status, job.started_at, job.worker_id = "running", datetime.now(UTC), worker_id
    db.commit()
    return job


def _window(db: Session, model, site_id: int, initial_days: int, resync_days: int) -> tuple[date, date]:
    end = date.today() - timedelta(days=1)
    last = db.scalar(select(func.max(model.date)).where(model.site_id == site_id))
    start = end - timedelta(days=initial_days - 1) if last is None else min(last - timedelta(days=resync_days - 1), end)
    return start, end


def _upsert(db: Session, model, rows: list[dict], constraint: str, update_cols: list[str]) -> None:
    for i in range(0, len(rows), UPSERT_BATCH):
        chunk = rows[i:i + UPSERT_BATCH]
        stmt = insert(model).values(chunk)
        stmt = stmt.on_conflict_do_update(constraint=constraint, set_={c: stmt.excluded[c] for c in update_cols})
        db.execute(stmt)


def _dedupe(rows: list[dict], key_cols: tuple[str, ...]) -> list[dict]:
    return list({tuple(r[k] for k in key_cols): r for r in rows}.values())


async def sync_search_console(db: Session, site: Site, client: httpx.AsyncClient | None = None) -> tuple[int, date, date]:
    s = get_settings()
    start, end = _window(db, SearchTotalsDaily, site.id, s.gsc_initial_days, GSC_RESYNC_DAYS)
    async with _client(client) as c:
        token = await access_token(db, c, site.org_id)
        totals = []
        async for r in search_console.iter_search_analytics(c, token, site.gsc_property, start, end,
                                                            dimensions=["date", "device"], data_state="all"):
            totals.append({"site_id": site.id, "date": date.fromisoformat(str(r["date"])), "device": r["device"],
                           "clicks": r["clicks"], "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"]})
        detail = []
        async for r in search_console.iter_search_analytics(c, token, site.gsc_property, start, end, data_state="all"):
            detail.append({"site_id": site.id, "date": date.fromisoformat(str(r["date"])), "page": r["page"],
                           "query": r["query"], "device": r["device"], "country": r["country"], "clicks": r["clicks"],
                           "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"]})
    cols = ["clicks", "impressions", "ctr", "position"]
    _upsert(db, SearchTotalsDaily, _dedupe(totals, ("date", "device")), "uq_gsc_totals", cols)
    _upsert(db, SearchPerformanceDaily, _dedupe(detail, ("date", "page", "query", "device", "country")), "uq_gsc_row", cols)
    db.commit()
    return len(totals) + len(detail), start, end


async def sync_analytics(db: Session, site: Site, client: httpx.AsyncClient | None = None, meta: dict | None = None) -> tuple[int, date, date]:
    s = get_settings()
    start, end = _window(db, OrganicLandingDaily, site.id, s.ga4_initial_days, GA4_RESYNC_DAYS)
    ce = site.conversion_events or {}
    events = {"signups": list(ce.get("signup") or []), "purchases": list(ce.get("purchase") or [])}
    async with _client(client) as c:
        token = await access_token(db, c, site.org_id)
        raw = await analytics.fetch_landing_daily(c, token, site.ga4_property_id, start, end, events, meta=meta)
    rows = [{
        "site_id": site.id, "date": date.fromisoformat(str(r["date"])), "landing_page": r["landing_page"] or "(not set)",
        "channel_group": r["channel_group"] or "(not set)", "source_medium": r["source_medium"] or "(not set)",
        "sessions": int(r.get("sessions") or 0), "total_users": int(r.get("total_users") or 0),
        "active_users": r.get("active_users"), "key_events": r.get("key_events"),
        "signups": r.get("signups"), "purchases": r.get("purchases"), "revenue": r.get("purchase_revenue"),
    } for r in raw]
    rows = _dedupe(rows, ("date", "landing_page", "channel_group", "source_medium"))
    _upsert(db, OrganicLandingDaily, rows, "uq_ga4_row",
            ["sessions", "total_users", "active_users", "key_events", "signups", "purchases", "revenue"])
    db.commit()
    return len(rows), start, end


async def run_sync_job(db: Session, job: SyncJob, client: httpx.AsyncClient | None = None) -> None:
    site = db.get(Site, job.site_id)
    try:
        meta: dict = {}
        if job.source == "search_console":
            n, start, end = await sync_search_console(db, site, client)
            meta = {"data_state": "all", "preliminary_from": (date.today() - timedelta(days=3)).isoformat()}
        else:
            n, start, end = await sync_analytics(db, site, client, meta)
        job.status, job.rows_synced, job.data_start, job.data_end, job.meta = "success", n, start, end, meta
    except Exception as exc:
        db.rollback()
        log.warning("sync job %s failed: %s", job.id, type(exc).__name__)
        job = db.get(SyncJob, job.id)
        job.status, job.error = "error", f"{type(exc).__name__}: {exc}"[:1000]
    job.finished_at = datetime.now(UTC)
    db.commit()


def integration_status(db: Session, site: Site, source: str) -> dict:
    prop = site.gsc_property if source == "search_console" else site.ga4_property_id
    jobs = db.scalars(select(SyncJob).where(SyncJob.site_id == site.id, SyncJob.source == source)
                      .order_by(SyncJob.id.desc()).limit(20)).all()
    last = jobs[0] if jobs else None
    success = next((j for j in jobs if j.status == "success"), None)
    model = SearchTotalsDaily if source == "search_console" else OrganicLandingDaily
    lo, hi = db.execute(select(func.min(model.date), func.max(model.date)).where(model.site_id == site.id)).one()
    return {
        "configured": bool(prop), "property": prop, "connected": get_credential(db, site.org_id) is not None,
        "last_sync_at": (last.finished_at or last.created_at).isoformat() if last else None,
        "last_success_at": success.finished_at.isoformat() if success and success.finished_at else None,
        "last_status": {"queued": "running", "running": "running"}.get(last.status, last.status) if last else "never",
        "last_error": last.error if last and last.status == "error" else None,
        "rows_synced": success.rows_synced if success else 0,
        "data_start": lo.isoformat() if lo else None, "data_end": hi.isoformat() if hi else None,
        "last_sync_metadata": success.meta if success else None,
    }
