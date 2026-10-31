import asyncio

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models import AuditFinding, CrawlPage, CrawlRun, PageLink, Site
from app.services import crawl_service
from app.worker import run_once
from tests.conftest import html

LONG = " ".join(["word"] * 300)


def fixture_routes(base_holder):
    def sm():
        b = base_holder["base"]
        return (200, {"Content-Type": "application/xml"},
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{b}/</loc></url>'
                f'<url><loc>{b}/about</loc></url><url><loc>{b}/orphan</loc></url><url><loc>{b}/gone</loc></url></urlset>')
    return {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /private\n"),
        "/": html("Home", f'<h1>Home</h1><a href="/about">About</a><a href="/blog?utm_source=x">Blog</a>'
                          f'<a href="/missing">Broken</a><a href="/private/x">Priv</a><a href="/old">Old</a>'
                          f'<a href="https://external.example/">Ext</a><p>{LONG}</p>',
                  '<meta name="description" content="home"><link rel="canonical" href="/">'),
        "/about": html("About", f"<h1>About</h1><p>{LONG}</p>", '<link rel="canonical" href="/about">'),
        "/blog": html("Home", "<p>thin</p>", '<meta name="robots" content="noindex">'),
        "/old": (301, {"Location": "/about"}, ""),
        "/orphan": html("Orphan", f"<h1>O</h1><p>{LONG}</p>"),
        "/gone": (410, {"Content-Type": "text/html"}, "gone"),
        "/private/x": html("Secret"),
        "__sitemap__": sm,
    }


def make_site(db, base, **kw) -> Site:
    from app.services.crawler.urls import host_of
    site = Site(org_id=1, project_id=1, name="Fixture", base_url=base + "/", allowed_hosts=[host_of(base)], conversion_events={}, crawl_settings=kw)
    db.add(site)
    db.commit()
    return site


def serve(fixture_site):
    holder = {}
    routes = fixture_routes(holder)
    sm = routes.pop("__sitemap__")
    site = fixture_site(routes)
    holder["base"] = site.base
    routes["/sitemap.xml"] = sm()
    return site


def test_full_crawl_via_api_and_worker(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    fx = serve(fixture_site)
    site = make_site(db, fx.base)
    r = client.post(f"/api/v1/sites/{site.id}/crawls", json={"crawl_settings": {"max_pages": 50}})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert client.post(f"/api/v1/sites/{site.id}/crawls").status_code == 409  # one active crawl per site
    assert asyncio.run(run_once("test-worker")) is True
    crawl = client.get(f"/api/v1/crawls/{r.json()['id']}").json()
    assert crawl["status"] == "completed" and crawl["sitemap_found"] is True
    assert "/private/x" not in fx.hits  # robots.txt respected
    urls = {p["normalized_url"].replace(fx.base, "") for p in client.get(f"/api/v1/crawls/{crawl['id']}/pages?page_size=200").json()["items"]}
    assert {"/", "/about", "/blog", "/old", "/orphan", "/gone", "/missing"} <= urls
    assert "/blog?utm_source=x" not in urls  # tracking param stripped → deduped
    summary = client.get(f"/api/v1/crawls/{crawl['id']}/summary").json()
    rules = {r["rule_id"] for r in summary["findings_by_rule"]}
    assert {"broken_internal_link", "noindex_meta", "http_4xx", "robots_blocked", "duplicate_title"} <= rules
    assert summary["status_distribution"]["200"] >= 3
    findings = client.get(f"/api/v1/crawls/{crawl['id']}/findings?severity=high,critical").json()
    assert findings["total"] > 0 and all(f["severity"] in ("high", "critical") for f in findings["items"])
    # page detail with raw evidence
    old = next(p for p in client.get(f"/api/v1/crawls/{crawl['id']}/pages?q=/old").json()["items"])
    detail = client.get(f"/api/v1/pages/{old['id']}").json()
    assert detail["redirect_chain"][0]["status_code"] == 301 and detail["final_url"].endswith("/about")
    # external link recorded but never fetched
    assert db.scalar(select(func.count()).select_from(PageLink).where(PageLink.is_internal.is_(False))) == 1


def test_reanalysis_is_idempotent(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    fx = serve(fixture_site)
    site = make_site(db, fx.base)
    cid = client.post(f"/api/v1/sites/{site.id}/crawls").json()["id"]
    asyncio.run(run_once("w"))
    before = client.get(f"/api/v1/crawls/{cid}/findings?page_size=200").json()
    client.post(f"/api/v1/crawls/{cid}/reanalyze")
    client.post(f"/api/v1/crawls/{cid}/reanalyze")
    after = client.get(f"/api/v1/crawls/{cid}/findings?page_size=200").json()
    assert before["total"] == after["total"] > 0
    key = lambda f: (f["rule_id"], f["url"], str(f["evidence"]))  # noqa: E731
    assert sorted(map(key, before["items"])) == sorted(map(key, after["items"]))


def test_loopback_crawl_blocked_when_private_disallowed(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=False)
    fx = serve(fixture_site)
    site = make_site(db, fx.base)  # inserted directly; the API would reject this base_url
    cid = client.post(f"/api/v1/sites/{site.id}/crawls").json()["id"]
    asyncio.run(run_once("w"))
    assert fx.hits == []
    pages = client.get(f"/api/v1/crawls/{cid}/pages").json()["items"]
    assert pages and all(p["fetch_error"] and p["fetch_error"].startswith("blocked") for p in pages)


def test_api_rejects_local_targets(client):
    for url in ["http://127.0.0.1/", "http://localhost:8000/", "http://10.1.2.3/", "http://[::ffff:127.0.0.1]/"]:
        r = client.post("/api/v1/sites", json={"name": "x", "base_url": url})
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_cancel_queued_and_running(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    fx = serve(fixture_site)
    site = make_site(db, fx.base)
    cid = client.post(f"/api/v1/sites/{site.id}/crawls").json()["id"]
    r = client.post(f"/api/v1/crawls/{cid}/cancel")
    assert r.json()["status"] == "cancelled"
    assert client.post(f"/api/v1/crawls/{cid}/cancel").status_code == 409
    assert asyncio.run(run_once("w")) is False and fx.hits == []
    # running crawl: cancel flag is observed by the engine loop
    cid = client.post(f"/api/v1/sites/{site.id}/crawls").json()["id"]
    with SessionLocal() as s:
        run = crawl_service.claim_next_crawl(s, "w")
        run.cancel_requested = True
        s.commit()
    asyncio.run(crawl_service.execute_crawl(cid))
    assert client.get(f"/api/v1/crawls/{cid}").json()["status"] == "cancelled"


def test_two_workers_cannot_claim_same_crawl(db, settings, fixture_site):
    fx = serve(fixture_site)
    site = make_site(db, fx.base)
    db.add(CrawlRun(site_id=site.id, status="queued", config=crawl_service.effective_crawl_config(site)))
    db.commit()
    s1, s2 = SessionLocal(), SessionLocal()
    try:
        # s1 holds the row lock (claim in progress); s2 must skip it
        row = s1.execute(select(CrawlRun).where(CrawlRun.status == "queued").with_for_update(skip_locked=True)).scalar_one()
        assert crawl_service.claim_next_crawl(s2, "w2") is None
        s1.rollback()
        assert crawl_service.claim_next_crawl(s2, "w2").id == row.id
        assert crawl_service.claim_next_crawl(s1, "w1") is None
    finally:
        s1.close()
        s2.close()


def test_stale_running_crawl_recovered(db):
    from datetime import UTC, datetime, timedelta
    site = make_site(db, "https://example.com")
    db.add(CrawlRun(site_id=site.id, status="running", config={}, heartbeat_at=datetime.now(UTC) - timedelta(hours=1)))
    db.commit()
    assert crawl_service.recover_stale_crawls(db) == 1
    assert db.scalar(select(CrawlRun.status)) == "failed"


def test_page_failure_does_not_abort_and_limits_apply(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True, crawler_max_pages_limit=3)
    fx = serve(fixture_site)
    site = make_site(db, fx.base)
    cid = client.post(f"/api/v1/sites/{site.id}/crawls", json={"crawl_settings": {"max_pages": 100}}).json()["id"]
    assert client.get(f"/api/v1/crawls/{cid}").json()["config"]["max_pages"] == 3  # env hard limit wins
    asyncio.run(run_once("w"))
    run = db.get(CrawlRun, cid)
    db.refresh(run)
    assert run.status == "completed" and run.truncated and run.pages_crawled == 3
    assert db.scalar(select(func.count()).select_from(CrawlPage)) == 3
    assert db.scalar(select(func.count()).select_from(AuditFinding).where(AuditFinding.rule_id == "sitemap_url_not_crawled")) == 0
