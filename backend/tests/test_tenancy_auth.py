import pytest
from sqlalchemy import func, select, text
from starlette.requests import Request

from app import auth
from app.api.v1.account import bootstrap_org
from app.core.security import totp_at
from app.db.session import SessionLocal
from app.models import ApiKey, AuditLog, CrawlRun, GrowthOpportunity, Site
from app.services import credits

PW = "correct horse battery staple"


@pytest.fixture()
def server(settings, db):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.api.v1 import account
    settings(app_mode="SERVER")
    account._login_bucket._local.clear()  # isolate the in-process login rate limiter per test
    with TestClient(app, base_url="https://testserver") as c:  # secure cookies need https
        yield c


def make_org(db, name, email, plan="starter", admin=False):
    org, user = bootstrap_org(db, name, email, PW, plan=plan, platform_admin=admin)
    return org, user


def login(client, email, totp=None):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PW, **({"totp_code": totp} if totp else {})})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def seed_site(db, org_id, url):
    from app.models import Project
    pid = db.scalar(select(Project.id).where(Project.org_id == org_id))
    s = Site(org_id=org_id, project_id=pid, name=url, base_url=url, allowed_hosts=[], conversion_events={}, crawl_settings={})
    db.add(s)
    db.flush()
    run = CrawlRun(site_id=s.id, status="completed", config={})
    db.add(run)
    db.add(GrowthOpportunity(site_id=s.id, key=f"k{s.id}", opportunity_type="low_ctr", title="t", description="d",
                             evidence={}, data_sources=[], confidence=0.5, effort="low", priority_score=1,
                             recommended_action="a", validation_metric="v", limitations="l"))
    db.commit()
    return s, run


def test_rls_blocks_cross_tenant_rows_at_db_level(db):
    org2, _ = make_org(db, "Org2", "b@example.com")
    s1, _ = seed_site(db, 1, "https://one.example/")
    s2, _ = seed_site(db, org2.id, "https://two.example/")
    with auth.scoped_session(org2.id) as sdb:
        assert {s.id for s in sdb.scalars(select(Site))} == {s2.id}
        assert sdb.scalar(select(func.count()).select_from(CrawlRun)) == 1  # child table chained through sites
        assert sdb.scalar(select(func.count()).select_from(GrowthOpportunity)) == 1
        sdb.add(Site(org_id=1, project_id=1, name="x", base_url="https://evil.example/", allowed_hosts=[],
                     conversion_events={}, crawl_settings={}))
        with pytest.raises(Exception, match="row-level security"):
            sdb.commit()
    with SessionLocal() as sysdb:  # system context (worker/CLI) sees all
        assert sysdb.scalar(select(func.count()).select_from(Site)) == 2


def test_cross_tenant_api_fuzz(server, db):
    org2, _ = make_org(db, "Org2", "b@example.com")
    s1, run1 = seed_site(db, 1, "https://one.example/")
    opp1 = db.scalar(select(GrowthOpportunity.id).where(GrowthOpportunity.site_id == s1.id))
    h = login(server, "b@example.com")
    probes = [("GET", f"/api/v1/sites/{s1.id}"), ("PATCH", f"/api/v1/sites/{s1.id}"), ("DELETE", f"/api/v1/sites/{s1.id}"),
              ("POST", f"/api/v1/sites/{s1.id}/crawls"), ("GET", f"/api/v1/crawls/{run1.id}"),
              ("GET", f"/api/v1/crawls/{run1.id}/findings"), ("GET", f"/api/v1/crawls/{run1.id}/summary"),
              ("GET", f"/api/v1/opportunities/{opp1}"), ("PATCH", f"/api/v1/opportunities/{opp1}"),
              ("GET", f"/api/v1/sites/{s1.id}/search-performance"), ("GET", f"/api/v1/sites/{s1.id}/competitors"),
              ("POST", f"/api/v1/sites/{s1.id}/competitors"), ("GET", f"/api/v1/sites/{s1.id}/growth"),
              ("POST", f"/api/v1/sites/{s1.id}/opportunities/generate")]
    for method, path in probes:
        r = server.request(method, path, headers=h, json={} if method in ("PATCH", "POST") else None)
        assert r.status_code in (404, 422), (method, path, r.status_code)
        assert "one.example" not in r.text
    assert server.get("/api/v1/sites", headers=h).json()["total"] == 0
    assert server.get("/api/v1/opportunities", headers=h).json()["total"] == 0


def test_server_mode_auth_csrf_rate_limit(server, db):
    make_org(db, "Org2", "b@example.com")
    assert server.get("/api/v1/sites").status_code == 401
    assert server.post("/api/v1/auth/login", json={"email": "b@example.com", "password": "wrong-password!"}).status_code == 401
    h = login(server, "b@example.com")
    assert server.get("/api/v1/sites").status_code == 200  # cookie session
    r = server.post("/api/v1/sites", json={"name": "x", "base_url": "https://x.example"})  # no CSRF header
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    assert server.post("/api/v1/sites", json={"name": "x", "base_url": "https://x.example"}, headers=h).status_code == 201
    server.post("/api/v1/auth/logout")
    assert server.get("/api/v1/sites").status_code == 401
    codes = [server.post("/api/v1/auth/login", json={"email": "c@example.com", "password": "nope-nope-nope"}).status_code
             for _ in range(7)]
    assert 429 in codes
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "auth.login_failed")) >= 1


def test_totp_flow(server, db):
    make_org(db, "Org2", "t@example.com")
    h = login(server, "t@example.com")
    secret = server.post("/api/v1/me/totp/setup", headers=h).json()["secret"]
    assert server.post("/api/v1/me/totp/enable", json={"code": "000000"}, headers=h).status_code == 400
    assert server.post("/api/v1/me/totp/enable", json={"code": totp_at(secret)}, headers=h).json()["totp_enabled"]
    server.post("/api/v1/auth/logout")
    r = server.post("/api/v1/auth/login", json={"email": "t@example.com", "password": PW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "totp_required"
    login(server, "t@example.com", totp=totp_at(secret))


def test_api_keys_roles_ip_and_revocation(server, db):
    make_org(db, "Org2", "b@example.com")
    h = login(server, "b@example.com")
    created = server.post("/api/v1/api-keys", json={"name": "ci", "role": "viewer"}, headers=h).json()
    key = created["key"]
    stored = db.scalar(select(ApiKey.key_hash).where(ApiKey.prefix == created["prefix"]))
    assert key not in stored and len(stored) == 64
    assert "key" not in server.get("/api/v1/api-keys", headers=h).json()["items"][0]
    server.cookies.clear()
    bearer = {"Authorization": f"Bearer {key}"}
    assert server.get("/api/v1/sites", headers=bearer).status_code == 200
    assert server.post("/api/v1/sites", json={"name": "x", "base_url": "https://x.example"}, headers=bearer).status_code == 403
    assert server.get("/api/v1/sites", headers={"Authorization": "Bearer sgi_deadbeef_nope"}).status_code == 401
    h = login(server, "b@example.com")
    limited = server.post("/api/v1/api-keys", json={"name": "ip", "ip_allowlist": ["203.0.113.9"]}, headers=h).json()
    server.post("/api/v1/auth/logout")
    assert server.get("/api/v1/sites", headers={"Authorization": f"Bearer {limited['key']}"}).status_code == 403
    h = login(server, "b@example.com")
    server.delete(f"/api/v1/api-keys/{created['id']}", headers=h)
    server.post("/api/v1/auth/logout")
    assert server.get("/api/v1/sites", headers=bearer).status_code == 401
    actions = set(db.scalars(select(AuditLog.action)))
    assert {"api_key.create", "api_key.revoke", "auth.login"} <= actions


def test_rbac_viewer_and_invites(server, db):
    make_org(db, "Org2", "owner@example.com")
    h = login(server, "owner@example.com")
    inv = server.post("/api/v1/invites", json={"email": "viewer@example.com", "role": "viewer"}, headers=h).json()
    assert inv["token"] and "not configured" in inv["delivery"]
    server.post("/api/v1/auth/logout")
    acc = server.post("/api/v1/auth/invites/accept", json={"token": inv["token"], "password": PW}).json()
    assert acc["role"] == "viewer"
    assert server.post("/api/v1/auth/invites/accept", json={"token": inv["token"], "password": PW}).status_code == 400
    hv = login(server, "viewer@example.com")
    assert server.get("/api/v1/sites", headers=hv).status_code == 200
    assert server.post("/api/v1/sites", json={"name": "x", "base_url": "https://x.example"}, headers=hv).status_code == 403
    assert server.get("/api/v1/members", headers=hv).status_code == 403
    assert server.get("/api/v1/admin/tenants", headers=hv).status_code == 403


def test_local_mode_rejects_non_loopback(settings):
    settings(app_mode="LOCAL_SINGLE_USER")
    req = Request({"type": "http", "method": "GET", "headers": [], "client": ("8.8.8.8", 1234), "path": "/"})
    with pytest.raises(Exception) as e:
        auth.resolve_principal(req)
    assert e.value.status_code == 403
    ok = Request({"type": "http", "method": "GET", "headers": [], "client": ("127.0.0.1", 1), "path": "/"})
    assert auth.resolve_principal(ok).role == "owner"


def test_credits_preflight_confirmation_limits(db):
    org, _ = make_org(db, "Paid", "p@example.com", plan="starter")
    pf = credits.preflight(db, org.id, "serp_request", 100)
    assert pf["estimated_credits"] == 100 and pf["allowed"] and pf["requires_confirmation"]
    with pytest.raises(PermissionError):
        credits.charge(db, org.id, "serp_request", 100, confirmed=False)
    assert credits.charge(db, org.id, "serp_request", 100, confirmed=True, idempotency_key="x1", provider_cost_usd=0.06) == 100
    assert credits.charge(db, org.id, "serp_request", 100, confirmed=True, idempotency_key="x1") == 0  # idempotent
    assert credits.balance(db, org.id) == 4900
    with pytest.raises(credits.InsufficientCredits):
        credits.charge(db, org.id, "backlink_query", 10_000, confirmed=True)
    for i in range(2):
        from app.models import Project
        db.add(Project(org_id=org.id, workspace_id=db.scalar(text("SELECT id FROM workspaces WHERE org_id=:o"), {"o": org.id}), name=f"p{i}"))
    db.commit()
    with pytest.raises(credits.PlanLimitExceeded):
        credits.check_limit(db, org.id, "projects")
    assert credits.balance(db, 1) is None  # local plan unlimited
    u = credits.usage(db, org.id)
    assert u["by_operation"][0]["provider_cost_usd"] == 0.06


def test_gdpr_export_delete_and_admin(server, db):
    org2, _ = make_org(db, "Org2", "b@example.com")
    make_org(db, "Platform", "admin@example.com", admin=True)
    seed_site(db, org2.id, "https://two.example/")
    seed_site(db, 1, "https://one.example/")
    h = login(server, "b@example.com")
    exp = server.get("/api/v1/org/export", headers=h).json()
    assert [s["base_url"] for s in exp["sites"]] == ["https://two.example/"] and "refresh" not in str(exp).lower()
    assert server.post("/api/v1/org/delete", json={"confirm_name": "wrong"}, headers=h).status_code == 422
    assert server.post("/api/v1/org/delete", json={"confirm_name": "Org2"}, headers=h).json()["deleted"]
    with SessionLocal() as sysdb:
        assert sysdb.scalar(select(func.count()).select_from(Site).where(Site.base_url == "https://two.example/")) == 0
        assert sysdb.scalar(select(func.count()).select_from(Site).where(Site.base_url == "https://one.example/")) == 1
    ha = login(server, "admin@example.com")
    tenants = server.get("/api/v1/admin/tenants", headers=ha).json()["items"]
    assert {t["name"] for t in tenants} >= {"Local", "Platform"}
    assert server.patch("/api/v1/admin/tenants/1", json={"suspended": True}, headers=ha).json()["suspended"] is True


def test_tenant_context_never_leaks_through_connection_pool(db):
    org2, _ = make_org(db, "Org2", "b@example.com")
    seed_site(db, 1, "https://one.example/")
    for _ in range(5):
        with auth.scoped_session(org2.id) as s:
            assert s.scalar(select(func.count()).select_from(Site)) == 0
    with SessionLocal() as s:  # next pooled user is back in system context
        assert s.scalar(text("SELECT app_org_id()")) is None
        assert s.scalar(select(func.count()).select_from(Site)) == 1
