from datetime import date, timedelta

from app.models import AuditFinding, CrawlPage, CrawlRun, OrganicLandingDaily, SearchPerformanceDaily, SearchTotalsDaily


def test_site_crud_validation_and_errors(client):
    r = client.post("/api/v1/sites", json={"name": "Shop", "base_url": "https://Shop.example.com",
                                           "allowed_hosts": ["cdn.example.com"], "ga4_property_id": "properties/42",
                                           "conversion_events": {"signup": ["sign_up"], "purchase": ["purchase"]}})
    assert r.status_code == 201
    site = r.json()
    assert site["base_url"] == "https://shop.example.com/" and site["allowed_hosts"] == ["shop.example.com", "cdn.example.com"]
    assert client.post("/api/v1/sites", json={"name": "Dup", "base_url": "https://shop.example.com/"}).status_code == 409
    for bad in [{"name": "x", "base_url": "ftp://a.com"}, {"name": "", "base_url": "https://a.com"},
                {"name": "x", "base_url": "https://a.com", "ga4_property_id": "abc"},
                {"name": "x", "base_url": "https://a.com", "conversion_events": {"signup": ["bad name!"]}},
                {"name": "x", "base_url": "https://a.com", "unknown": 1},
                {"name": "x", "base_url": "https://a.com", "crawl_settings": {"max_pages": 0}}]:
        r = client.post("/api/v1/sites", json=bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", bad
    r = client.patch(f"/api/v1/sites/{site['id']}", json={"gsc_property": "sc-domain:example.com", "crawl_settings": {"max_pages": 20}})
    assert r.json()["gsc_property"] == "sc-domain:example.com" and r.json()["crawl_settings"] == {"max_pages": 20}
    lst = client.get("/api/v1/sites", params={"page_size": 1}).json()
    assert lst["total"] == 1 and lst["page_size"] == 1 and lst["items"][0]["last_crawl"] is None
    assert client.get("/api/v1/sites/999").json()["error"]["code"] == "not_found"
    assert client.delete(f"/api/v1/sites/{site['id']}").status_code == 204
    assert client.get(f"/api/v1/sites/{site['id']}").status_code == 404


def seed(db, site_id):
    """Synthetic GSC/GA4 data: clear low-CTR, declining, low-conversion and indexing-issue signals."""
    end = date.today() - timedelta(days=3)
    cur = [end - timedelta(days=i) for i in range(28)]
    prev = [end - timedelta(days=28 + i) for i in range(28)]
    rows = []
    for d in cur:
        # 10 comparable queries in position bucket 1-3 with healthy CTR, one with very low CTR
        for q in range(10):
            rows.append(SearchPerformanceDaily(site_id=site_id, date=d, page=f"https://example.com/p{q}", query=f"q{q}",
                                               device="DESKTOP", country="usa", clicks=10, impressions=50, ctr=0.2, position=2.0))
        rows.append(SearchPerformanceDaily(site_id=site_id, date=d, page="https://example.com/low", query="low ctr query",
                                           device="DESKTOP", country="usa", clicks=0, impressions=100, ctr=0.0, position=2.0))
        rows.append(SearchPerformanceDaily(site_id=site_id, date=d, page="https://example.com/decl", query="decl",
                                           device="DESKTOP", country="usa", clicks=1, impressions=20, ctr=0.05, position=5.0))
        rows.append(SearchTotalsDaily(site_id=site_id, date=d, device="DESKTOP", clicks=120, impressions=700, ctr=0.17, position=3.0))
    for d in prev:
        rows.append(SearchPerformanceDaily(site_id=site_id, date=d, page="https://example.com/decl", query="decl",
                                           device="DESKTOP", country="usa", clicks=10, impressions=50, ctr=0.2, position=3.0))
        rows.append(SearchTotalsDaily(site_id=site_id, date=d, device="DESKTOP", clicks=100, impressions=600, ctr=0.17, position=3.0))
    for i, d in enumerate(cur):
        for lp in range(5):
            rows.append(OrganicLandingDaily(site_id=site_id, date=d, landing_page=f"/lp{lp}", channel_group="Organic Search",
                                            source_medium="google / organic", sessions=10, total_users=9, signups=1, purchases=0))
        rows.append(OrganicLandingDaily(site_id=site_id, date=d, landing_page="/p0", channel_group="Organic Search",
                                        source_medium="google / organic", sessions=10, total_users=9, signups=0, purchases=0))
    db.add_all(rows)
    run = CrawlRun(site_id=site_id, status="completed", config={})
    db.add(run)
    db.flush()
    page = CrawlPage(crawl_run_id=run.id, url="https://example.com/p0", normalized_url="https://example.com/p0", depth=1)
    db.add(page)
    db.flush()
    db.add(AuditFinding(crawl_run_id=run.id, page_id=page.id, url=page.normalized_url, rule_id="noindex_meta",
                        severity="high", title="noindex", description="d", evidence={}, remediation="r", fingerprint="f1"))
    db.commit()


def test_growth_opportunities_from_synthetic_data(client, db):
    site = client.post("/api/v1/sites", json={"name": "Ex", "base_url": "https://example.com"}).json()
    seed(db, site["id"])
    res = client.post(f"/api/v1/sites/{site['id']}/opportunities/generate").json()
    assert res["total"] > 0 and res["created"] == res["total"]
    opps = client.get(f"/api/v1/sites/{site['id']}/opportunities", params={"page_size": 200}).json()["items"]
    types = {o["opportunity_type"] for o in opps}
    assert {"low_ctr", "declining_performance", "indexing_issue_on_valuable_page", "low_conversion"} <= types
    for o in opps:
        assert o["data_sources"] and o["recommended_action"] and o["validation_metric"] and o["limitations"]
        assert 0 <= o["confidence"] <= 1 and o["effort"] in ("low", "medium", "high") and o["date_range"]
    low = next(o for o in opps if o["opportunity_type"] == "low_ctr")
    assert low["url"] == "https://example.com/low" and low["impact_estimate"]["value"] > 0
    pri = [o["priority_score"] for o in opps]
    assert pri == sorted(pri, reverse=True)
    # status workflow + implemented_at tracking; regeneration preserves operator status
    r = client.patch(f"/api/v1/opportunities/{low['id']}", json={"status": "implemented", "notes": "new title"})
    assert r.json()["status"] == "implemented" and r.json()["implemented_at"]
    again = client.post(f"/api/v1/sites/{site['id']}/opportunities/generate").json()
    assert again["created"] == 0 and again["updated"] == res["total"]
    assert client.get(f"/api/v1/opportunities/{low['id']}").json()["status"] == "implemented"
    assert client.patch(f"/api/v1/opportunities/{low['id']}", json={"status": "bogus"}).status_code == 422
    flt = client.get("/api/v1/opportunities", params={"status": "implemented"}).json()
    assert flt["total"] == 1 and flt["items"][0]["site_name"] == "Ex"
    for sort in ("confidence", "impact", "effort", "created"):
        assert client.get("/api/v1/opportunities", params={"sort": sort}).status_code == 200
    # dashboard: real numbers from synthetic rows, GA4 and GSC kept separate
    ov = client.get("/api/v1/dashboard/overview").json()
    s = ov["sites"][0]
    assert s["current"]["clicks"] == 120 * 28 and s["current"]["sessions"] == 60 * 28 and s["previous"]["sessions"] is None
    g = client.get(f"/api/v1/sites/{site['id']}/growth").json()
    assert g["change_pct"]["clicks"] == 20.0 and g["change_pct"]["sessions"] is None


def test_no_data_means_no_opportunities(client, db):
    site = client.post("/api/v1/sites", json={"name": "Empty", "base_url": "https://empty.example"}).json()
    assert client.post(f"/api/v1/sites/{site['id']}/opportunities/generate").json()["total"] == 0
    ov = client.get("/api/v1/dashboard/overview").json()
    assert ov["totals"]["clicks"] is None and ov["sites"][0]["critical_findings"] is None
