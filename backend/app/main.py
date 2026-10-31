import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import (account, automation, backlinks, code_agent, competitors, content_ai, data, diagnosis_api, domains, insights, keywords,
                        link_building, ppc, rank, site_ops, sites, social, system)
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import SessionLocal

setup_logging()
settings = get_settings()
app = FastAPI(title="SEO Growth Intelligence", version=settings.app_version)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST", "PATCH", "DELETE"],
                   allow_headers=["Content-Type", "X-CSRF-Token", "X-Org-Id", "Authorization"], allow_credentials=True)


@app.middleware("http")
async def idempotency(request: Request, call_next):
    """Idempotency-Key on unsafe methods: the first response (status+JSON body) is replayed for 24 h."""
    key = request.headers.get("idempotency-key")
    if not key or request.method in ("GET", "HEAD", "OPTIONS") or len(key) > 128:
        return await call_next(request)
    import hashlib

    from app.models import IdempotencyKey
    who = request.headers.get("authorization") or request.cookies.get("sgi_session") or (request.client.host if request.client else "")
    k = hashlib.sha256(f"{who}|{request.method}|{request.url.path}|{key}".encode()).hexdigest()
    with SessionLocal() as db:
        hit = db.get(IdempotencyKey, k)
        if hit is not None:
            return JSONResponse(status_code=hit.status_code, content=__import__("json").loads(hit.body),
                                headers={"Idempotent-Replay": "true"})
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("application/json") and resp.status_code < 500:
        body = b"".join([chunk async for chunk in resp.body_iterator])
        with SessionLocal() as db:
            db.merge(IdempotencyKey(key=k, status_code=resp.status_code, body=body.decode()))
            db.commit()
        return Response(content=body, status_code=resp.status_code, headers=dict(resp.headers), media_type="application/json")
    return resp


@app.middleware("http")
async def observe(request: Request, call_next):
    """Prometheus RED metrics per route template + W3C traceparent propagation (trace id echoed in response)."""
    import time

    from app.infra import observability as obs
    trace_id, _ = obs.start_trace(request.headers.get("traceparent"))
    t0 = time.perf_counter()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        resp.headers["X-Trace-Id"] = trace_id
        return resp
    finally:
        route = request.scope.get("route")
        tmpl = getattr(route, "path", None) or "unmatched"
        obs.HTTP_REQUESTS.inc(route=tmpl, method=request.method, status=f"{status // 100}xx")
        obs.HTTP_LATENCY.observe(time.perf_counter() - t0, route=tmpl, method=request.method)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(request: Request):
    """Prometheus scrape endpoint. Loopback-only unless METRICS_TOKEN is configured (Bearer)."""
    import hmac

    from app.infra import observability as obs
    token = settings.metrics_token
    host = request.client.host if request.client else ""
    if token:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, token):
            return _err(401, "unauthorized", "metrics token required")
    elif host not in ("127.0.0.1", "::1", "testclient", "localhost"):
        return _err(403, "forbidden", "metrics are loopback-only without METRICS_TOKEN")
    obs.sample_gauges()
    return Response(obs.REGISTRY.render(), media_type="text/plain; version=0.0.4")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    return resp

_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 409: "conflict", 422: "validation_error"}


def _err(status: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details}})


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return _err(exc.status_code, exc.detail["code"], exc.detail["message"], exc.detail.get("details"))
    return _err(exc.status_code, _CODES.get(exc.status_code, "error"), str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    details = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return _err(422, "validation_error", "request validation failed", details)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    logging.getLogger("app").exception("unhandled error: %s", type(exc).__name__)
    return _err(500, "internal_error", "internal server error")


@app.get("/ready")
def ready():
    from app.api.v1.system import readiness
    r = readiness()
    return JSONResponse(status_code=200 if r["ready"] else 503, content=r)


@app.get("/health")
def health():
    db_status = "ok"
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"
    return {"status": "ok", "database": db_status, "version": settings.app_version}


for r in (account.router, sites.router, data.router, competitors.router, system.router, keywords.router,
          keywords.lists_router, rank.router, domains.router, site_ops.router,
          backlinks.router, link_building.router, ppc.router, social.router, diagnosis_api.router, code_agent.router, insights.router, content_ai.router, automation.router):
    app.include_router(r, prefix="/api/v1")


@app.get("/share/{token}", response_class=HTMLResponse, include_in_schema=False)
def shared_report(token: str):
    """Public, read-only, expiring, revocable report link (no auth; scoped to the link's organization)."""
    from app.auth import scoped_session
    from app.models import Organization, ShareLink, Site
    from app.services import automation as auto
    with SessionLocal() as sysdb:
        link = auto.resolve_share(sysdb, token)
        if link is None or not link.resource.startswith("report:site:"):
            return HTMLResponse("<h1>Link expired or revoked</h1>", status_code=404)
        org_id, site_id = link.org_id, int(link.resource.split(":")[2])
    with scoped_session(org_id) as db:
        site = db.get(Site, site_id)
        if site is None:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)
        html = auto.render_report_html(auto.weekly_digest(db, site), db.get(Organization, org_id).branding)
    return HTMLResponse(html, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                                       "Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
