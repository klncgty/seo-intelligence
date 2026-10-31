import asyncio
import json

import respx
from sqlalchemy import func, select, text

from app.models import BotHitDaily, CrawlRun, IntegrationCredential, Site
from app.services import site_ops
from tests.conftest import html


def make_site(db, base, **kw):
    s = Site(org_id=1, project_id=1, name="s", base_url=base + "/", allowed_hosts=[base.split("//")[1].split(":")[0]],
             conversion_events={}, crawl_settings={}, **kw)
    db.add(s)
    db.commit()
    return s


def test_verification_methods(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    site = make_site(db, "http://127.0.0.1:1")
    tok = client.post(f"/api/v1/sites/{site.id}/verification").json()["token"]
    fx = fixture_site({"/": html("Home", head=f'<meta name="seo-growth-verification" content="{tok}">'),
                       f"/seo-growth-verification-{tok}.txt": (200, {"Content-Type": "text/plain"}, tok)})
    db.execute(text("UPDATE sites SET base_url = :b WHERE id = :i"), {"b": fx.base + "/", "i": site.id})
    db.commit()
    db.refresh(site)
    assert client.post(f"/api/v1/sites/{site.id}/verification/check", json={"method": "html_meta"}).json()["verified"]
    db.execute(text("UPDATE sites SET verified_at = NULL WHERE id = :i"), {"i": site.id}); db.commit(); db.refresh(site)
    assert asyncio.run(site_ops.check_verification(db, site, "file"))["verified"]
    site.verified_at = None; db.commit()
    assert asyncio.run(site_ops.check_verification(db, site, "dns_txt", txt_lookup=lambda h: [f"seo-growth-verification={tok}"]))["verified"]
    site.verified_at = None; db.commit()
    assert not asyncio.run(site_ops.check_verification(db, site, "dns_txt", txt_lookup=lambda h: ["other"]))["verified"]
    site.gsc_property = "sc-domain:example.com"; db.commit()
    assert asyncio.run(site_ops.check_verification(db, site, "gsc", gsc_sites=["sc-domain:example.com"]))["verified"]


def test_unverified_crawl_is_shallow(client, db, settings):
    settings(unverified_max_pages=7, unverified_max_depth=2)
    s = client.post("/api/v1/sites", json={"name": "x", "base_url": "https://x.example"}).json()
    cfg = client.post(f"/api/v1/sites/{s['id']}/crawls", json={"crawl_settings": {"max_pages": 100, "max_depth": 9}}).json()["config"]
    assert cfg["max_pages"] == 7 and cfg["max_depth"] == 2 and cfg["unverified_limits"] is True


LOG = """66.249.66.1 - - [28/Oct/2026:10:00:00 +0000] "GET /urun/1 HTTP/1.1" 200 512 "-" "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
66.249.66.1 - - [28/Oct/2026:10:05:00 +0000] "GET /eski HTTP/1.1" 404 0 "-" "Mozilla/5.0 (compatible; Googlebot/2.1)"
203.0.113.7 - - [28/Oct/2026:11:00:00 +0000] "GET /urun/1 HTTP/1.1" 200 512 "-" "Mozilla/5.0 (compatible; Googlebot/2.1) fake"
198.51.100.23 - - [28/Oct/2026:12:00:00 +0000] "GET /sepet HTTP/1.1" 200 100 "-" "Mozilla/5.0 (Macintosh) Safari"
garbage line
"""


def test_log_analysis_verifies_googlebot_and_drops_humans(client, db):
    site = make_site(db, "https://shop.example")
    assert client.post(f"/api/v1/sites/{site.id}/logs", content=LOG.encode()).status_code == 403  # unverified
    rdns = {"66.249.66.1": "crawl-66-249-66-1.googlebot.com", "203.0.113.7": "evil.example"}.get
    res = site_ops.ingest_log(db, site, LOG.splitlines(), rdns=rdns, forward=lambda h: {"66.249.66.1"})
    assert res["lines_parsed"] == 4 and res["lines_skipped"] == 1 and res["non_bot_lines_discarded"] == 1
    stored = db.scalars(select(BotHitDaily)).all()
    assert all("198.51.100.23" not in json.dumps([r.path, r.bot]) for r in stored)
    assert {(r.path, r.verified) for r in stored} == {("/urun/1", True), ("/eski", True), ("/urun/1", False)}
    db.add(CrawlRun(site_id=site.id, status="completed", config={}, sitemap_urls=["https://shop.example/urun/1",
                                                                                    "https://shop.example/urun/2"]))
    db.commit()
    rep = client.get(f"/api/v1/sites/{site.id}/logs/report", params={"days": 30}).json()
    assert rep["status_codes_to_googlebot"] == {"200": 1, "404": 1} and rep["unverified_googlebot_hits"] == 1
    assert rep["important_not_crawled"] == ["/urun/2"]


def test_migration_validation(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    fx = fixture_site({"/eski-1": (301, {"Location": "/yeni-1"}, ""), "/yeni-1": html("Y1"),
                       "/eski-2": (302, {"Location": "/yeni-2"}, ""), "/yeni-2": html("Y2"),
                       "/eski-3": (301, {"Location": "/ara"}, ""), "/ara": (301, {"Location": "/yeni-3"}, ""), "/yeni-3": html("Y3"),
                       "/eski-4": html("still here")})
    site = make_site(db, fx.base)
    b = fx.base
    csv = f"from,to\n{b}/eski-1,{b}/yeni-1\n{b}/eski-2,{b}/yeni-2\n{b}/eski-3,{b}/yeni-3\n{b}/eski-4,{b}/yeni-4\nhttps://evil.example/x,{b}/y\n"
    res = client.post(f"/api/v1/sites/{site.id}/migration/validate", json={"csv": csv}).json()
    by = {r["from"].replace(b, ""): r for r in res["results"]}
    assert by["/eski-1"]["ok"] and res["ok"] == 1 and res["failed"] == 4
    assert any("302" in i for i in by["/eski-2"]["issues"]) and any("2 hops" in i for i in by["/eski-3"]["issues"])
    assert by["/eski-4"]["issues"] == ["no redirect"] and by["https://evil.example/x"]["issues"][0].startswith("blocked")


@respx.mock
def test_cwv_not_connected_and_crux_parsing(client, db, settings):
    site = make_site(db, "https://shop.example")
    r = client.get(f"/api/v1/sites/{site.id}/cwv").json()
    assert r["field"]["origin"]["available"] is False and "CRUX_API_KEY" in r["field"]["origin"]["reason"]
    settings(crux_api_key="k")
    respx.post("https://chromeuxreport.googleapis.com/v1/records:queryRecord").respond(json={"record": {"metrics": {
        "largest_contentful_paint": {"percentiles": {"p75": 2300}}}, "collectionPeriod": {"lastDate": {"year": 2026}}}})
    respx.post("https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord").respond(404)
    r = client.get(f"/api/v1/sites/{site.id}/cwv").json()
    assert r["field"]["origin"]["p75"] == {"largest_contentful_paint": 2300}
    assert r["field"]["history"]["reason"].startswith("insufficient real-user traffic")


@respx.mock
def test_url_inspection_budget_and_canonical_conflict(client, db, settings):
    from app.core import crypto
    settings(google_client_id="cid", google_client_secret="sec", url_inspection_daily_quota=2)
    site = make_site(db, "https://shop.example", gsc_property="sc-domain:shop.example")
    db.add(IntegrationCredential(org_id=1, provider="google", encrypted_refresh_token=crypto.encrypt("1//x"), scopes=[]))
    db.commit()
    respx.post("https://oauth2.googleapis.com/token").respond(json={"access_token": "ya29.a", "expires_in": 3600, "scope": ""})
    respx.post(site_ops.INSPECT).respond(json={"inspectionResult": {"indexStatusResult": {
        "verdict": "PASS", "googleCanonical": "https://shop.example/a", "userCanonical": "https://shop.example/a?x=1"}}})
    r = client.post(f"/api/v1/sites/{site.id}/url-inspection", json={"urls": ["https://shop.example/a", "https://shop.example/b",
                                                                           "https://shop.example/c"]}).json()
    assert r["inspected"] == 2 and r["skipped_over_budget"] == 1 and r["results"][0]["canonical_conflict"] is True
    r2 = client.post(f"/api/v1/sites/{site.id}/url-inspection", json={"urls": ["https://shop.example/d"]}).json()
    assert r2["inspected"] == 0 and r2["remaining_budget_today"] == 0
