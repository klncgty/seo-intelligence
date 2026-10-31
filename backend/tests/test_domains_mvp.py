"""Domain & competitor intelligence + MVP gate (TR locale, one provider = DEMO mock): onboarding → audit-ready site →
GSC (mocked rows) → competitor suggestion/confirm → keywords → rank tracking → keyword gap → opportunity → status."""

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.infra.clickhouse import ClickHouse
from app.models import CrawlPage, CrawlRun, SearchPerformanceDaily
from app.providers import registry, runtime
from app.services import domains as dom
from app.worker import run_once

DB = "tr-TR:google.com.tr"
KWS = ["kahve makinesi fiyatları", "en iyi kahve makinesi", "espresso makinesi fiyat", "türk kahvesi makinesi en iyi",
       "filtre kahve makinesi fiyatları", "kahve öğütücü fiyat", "en iyi espresso makinesi", "kapsül kahve makinesi fiyat"]


@pytest.fixture()
def ch(settings, db):
    c = ClickHouse(url="http://127.0.0.1:8123", database="seo_test")
    if not c.ping():
        pytest.skip("ClickHouse not running")
    c.command("DROP DATABASE IF EXISTS seo_test", database="default")
    c.migrate()
    settings(clickhouse_url="http://127.0.0.1:8123", clickhouse_database="seo_test", minio_endpoint="")
    registry.clear_overrides()
    runtime.BREAKER.failures.clear(); runtime.BREAKER.opened_at.clear(); runtime.BUCKET._local.clear()
    db.execute(text("UPDATE organizations SET is_demo = true WHERE id = 1"))
    db.commit()
    return c


def test_ctr_calibration_from_own_gsc(client, db):
    site = client.post("/api/v1/sites", json={"name": "Ex", "base_url": "https://example.com.tr"}).json()
    assert client.get(f"/api/v1/sites/{site['id']}/traffic-model").json()["available"] is False
    d = date.today() - timedelta(days=3)
    for i, (pos, clicks, impr) in enumerate([(1, 300, 1000), (1, 250, 1000), (3, 90, 1000), (8, 20, 1000), (30, 1, 1000)]):
        db.add(SearchPerformanceDaily(site_id=site["id"], date=d, page="https://example.com.tr/", query=f"q{i}", device="MOBILE",
                                      country="tur", clicks=clicks, impressions=impr, ctr=clicks / impr, position=pos))
    db.commit()
    cal = client.get(f"/api/v1/sites/{site['id']}/traffic-model").json()
    assert cal["available"] and cal["curve"] == {"1": 0.275, "3": 0.09, "8": 0.02}
    assert cal["queries_used"] == 4 and cal["median_ape"] is not None  # position 30 excluded


def test_competitor_digest(db):
    from app.models import Competitor, Site
    s = Site(org_id=1, project_id=1, name="s", base_url="https://me.com/", allowed_hosts=[], conversion_events={}, crawl_settings={})
    db.add(s); db.flush()
    c = Competitor(site_id=s.id, name="r", domain="rival.com", base_url="https://rival.com/", allowed_hosts=["rival.com"])
    db.add(c); db.flush()
    from datetime import UTC, datetime
    r1 = CrawlRun(site_id=s.id, competitor_id=c.id, status="completed", config={}, sitemap_urls=["https://rival.com/a"],
                  finished_at=datetime(2026, 10, 1, tzinfo=UTC))
    r2 = CrawlRun(site_id=s.id, competitor_id=c.id, status="completed", config={}, sitemap_urls=["https://rival.com/a", "https://rival.com/b"],
                  finished_at=datetime(2026, 10, 8, tzinfo=UTC))
    db.add_all([r1, r2]); db.flush()
    for run, url, title, h in [(r1, "https://rival.com/a", "A", "1"), (r1, "https://rival.com/pricing", "Plans", "x"),
                               (r2, "https://rival.com/a", "A new", "1"), (r2, "https://rival.com/pricing", "Plans", "y"),
                               (r2, "https://rival.com/b", "B", "2")]:
        db.add(CrawlPage(crawl_run_id=run.id, url=url, normalized_url=url, title=title, content_hash=h, depth=0))
    db.commit()
    dg = dom.competitor_digest(db, c.id)
    assert dg["new_urls"] == ["https://rival.com/b"] and dg["title_changes"][0]["to"] == "A new"
    assert dg["pricing_changes"] == [{"url": "https://rival.com/pricing", "content_changed": True}]
    assert dg["sitemap"]["added"] == ["https://rival.com/b"] and dg["publishing_velocity_per_day"] == round(1 / 7, 2)


def test_mvp_gate_end_to_end(client, db, ch):
    # 1) onboarding: project/site
    site = client.post("/api/v1/sites", json={"name": "Örnek", "base_url": "https://example.com.tr"}).json()
    # 2) GSC connected (mocked first-party rows — real OAuth needs credentials)
    d = date.today() - timedelta(days=4)
    db.add(SearchPerformanceDaily(site_id=site["id"], date=d, page="https://example.com.tr/kahve", query="kahve makinesi fiyatları",
                                  device="MOBILE", country="tur", clicks=4, impressions=220, ctr=0.018, position=12.3))
    db.commit()
    # 3) keywords: metrics + SERP snapshots (credit preflight confirmed)
    assert client.post("/api/v1/keywords/lookup", json={"keywords": KWS, "confirm": True}).status_code == 200
    assert client.post("/api/v1/keywords/serps", json={"keywords": KWS, "confirm": True}).json()["snapshots"] == len(KWS)
    # 4) competitor suggestion (SERP overlap) → explicit confirmation
    sugg = client.get(f"/api/v1/sites/{site['id']}/competitor-suggestions/serp").json()["suggestions"]
    assert sugg and all(s["shared_top10_keywords"] >= 1 for s in sugg)
    rivals = [s["domain"] for s in sugg if s["domain"] in ("rakip1.com.tr", "rakip2.com")][:2]
    assert rivals
    for r in rivals:
        assert client.post(f"/api/v1/sites/{site['id']}/competitors", json={"name": r, "domain": r}).status_code == 201
    # 5) rank tracking
    rp = client.post("/api/v1/rank-projects", json={"name": "TR", "own_domain": "example.com.tr", "competitor_domains": rivals}).json()
    client.post(f"/api/v1/rank-projects/{rp['id']}/keywords", json={"keywords": KWS})
    client.post(f"/api/v1/rank-projects/{rp['id']}/confirm-budget")
    client.post(f"/api/v1/rank-projects/{rp['id']}/check")
    for _ in range(6):
        asyncio.run(run_once("w"))
    assert client.get(f"/api/v1/rank-projects/{rp['id']}/overview").json()["available"]
    # 6) domain overview: observed vs provider vs actual kept apart
    ov = client.get("/api/v1/domains/example.com.tr/overview").json()
    assert ov["observed"]["source_class"] == "OBSERVED" and ov["observed"]["ranked_keywords"] > 0
    assert ov["provider"]["available"] is False and ov["actual"]["source_class"] == "FIRST_PARTY"
    assert ov["unsupported"]["clickstream"].startswith("not available")
    # 7) keyword gap (2–5 domains) with own GSC layer separate
    gap = client.post("/api/v1/domains/keyword-gap", json={"domains": ["example.com.tr", *rivals], "site_id": site["id"]}).json()
    assert gap["counts"]["missing"] + gap["counts"]["untapped"] > 0 and gap["own_gsc_layer"]["source_class"] == "FIRST_PARTY"
    assert client.post("/api/v1/domains/keyword-gap", json={"domains": ["example.com.tr"]}).status_code == 422
    # 8) opportunity from the gap → status change
    res = client.post("/api/v1/domains/keyword-gap/opportunities", json={"domains": ["example.com.tr", *rivals], "site_id": site["id"]}).json()
    assert res["created"] > 0
    opps = client.get(f"/api/v1/sites/{site['id']}/opportunities", params={"type": "keyword_gap"}).json()["items"]
    o = opps[0]
    assert o["impact_estimate"]["source_class"] == "MODELED" and o["impact_estimate"]["p10"] <= o["impact_estimate"]["p50"] <= o["impact_estimate"]["p90"]
    assert "does not prove" in o["limitations"] and o["data_sources"]
    again = client.post("/api/v1/domains/keyword-gap/opportunities", json={"domains": ["example.com.tr", *rivals], "site_id": site["id"]}).json()
    assert again["created"] == 0  # idempotent
    assert client.patch(f"/api/v1/opportunities/{o['id']}", json={"status": "planned"}).json()["status"] == "planned"


def test_module1_domain_analysis_and_visibility(client, db, ch):
    from datetime import UTC, datetime
    from app.models import DomainAnalysisRecord
    from sqlalchemy import select

    site = client.post("/api/v1/sites", json={"name": "Mod1 Site", "base_url": "https://example.com.tr"}).json()
    run = CrawlRun(
        site_id=site["id"],
        status="completed",
        config={},
        sitemap_found=True,
        sitemap_urls=["https://example.com.tr/", "https://example.com.tr/urunler/kahve-makinesi?sort=price", "https://example.com.tr/Blog_Yazisi"],
        robots_blocked_urls=["https://example.com.tr/admin"],
        robots_txt="User-agent: *\nDisallow: /admin\nDisallow: /assets/*.js\nCrawl-delay: 2\nSitemap: https://example.com.tr/sitemap.xml\n",
        finished_at=datetime.now(UTC),
    )
    db.add(run)
    db.flush()
    db.add_all([
        CrawlPage(crawl_run_id=run.id, url="https://example.com.tr/", normalized_url="https://example.com.tr/", status_code=200, is_html=True, content_type="text/html", word_count=950, depth=0),
        CrawlPage(crawl_run_id=run.id, url="https://example.com.tr/urunler/kahve-makinesi", normalized_url="https://example.com.tr/urunler/kahve-makinesi", status_code=200, is_html=True, content_type="text/html", word_count=220, depth=1),
        CrawlPage(crawl_run_id=run.id, url="https://example.com.tr/eski", normalized_url="https://example.com.tr/eski", status_code=200, is_html=True, meta_robots="noindex", word_count=100, depth=1),
        CrawlPage(crawl_run_id=run.id, url="https://example.com.tr/404", normalized_url="https://example.com.tr/404", status_code=404, is_html=True, word_count=50, depth=1),
    ])
    d_today = date.today()
    d_prev = d_today - timedelta(days=5)
    db.add(SearchPerformanceDaily(site_id=site["id"], date=d_today, page="https://example.com.tr/", query="kahve makinesi fiyatları", device="MOBILE", country="tur", clicks=15, impressions=300, ctr=0.05, position=3.2))
    db.commit()

    # Lookup keywords and insert historical + current rankings to test visibility changes
    assert client.post("/api/v1/keywords/lookup", json={"keywords": KWS[:4], "confirm": True}).status_code == 200
    assert client.post("/api/v1/keywords/serps", json={"keywords": KWS[:4], "confirm": True}).status_code == 200
    ch.insert("domain_keyword_rankings", [
        {"registrable_domain": "example.com.tr", "locale": DB, "device": "desktop", "keyword_key": "kahv makine fiyat", "snapshot_date": d_prev.isoformat(), "best_rank": 8, "url": "https://example.com.tr/", "version": 1},
        {"registrable_domain": "example.com.tr", "locale": DB, "device": "desktop", "keyword_key": "eski kelim", "snapshot_date": d_prev.isoformat(), "best_rank": 12, "url": "https://example.com.tr/eski", "version": 1},
        {"registrable_domain": "example.com.tr", "locale": DB, "device": "mobile", "keyword_key": "kahv makine fiyat", "snapshot_date": d_today.isoformat(), "best_rank": 2, "url": "https://example.com.tr/", "version": 2},
    ])

    ov = client.get("/api/v1/domains/example.com.tr/overview").json()
    assert ov["observed"]["estimated_traffic"]["isEstimated"] is True
    assert ov["observed"]["estimated_traffic"]["confidenceLevel"] in ("HIGH", "MEDIUM", "LOW")
    assert "estimated_traffic_value" in ov["observed"]
    assert ov["observed"]["visibility_score"]["value"] is not None
    assert len(ov["observed"]["top_keywords"]) >= 1
    assert ov["indexed_pages"]["crawled_indexable_pages"] == 2
    assert ov["country_distribution"]["first_party_gsc_countries"][0]["country"] == "TUR"
    assert len(ov["device_distribution"]["observed_by_device"]) >= 1
    assert ov["visibility"]["ranking_changes"]["counts"]["lost"] >= 1

    vis_ep = client.get("/api/v1/domains/example.com.tr/visibility").json()
    assert vis_ep["domain"] == "example.com.tr"
    assert "ranking_changes" in vis_ep["visibility"]

    tech = client.get("/api/v1/domains/example.com.tr/technical-analysis", params={"fetch_live": False}).json()
    assert tech["robots_txt"]["reachable"] is True
    assert tech["robots_txt"]["sitemaps_declared"] == ["https://example.com.tr/sitemap.xml"]
    assert any("CSS/JS" in iss for iss in tech["robots_txt"]["issues"])
    assert tech["sitemap"]["sitemap_found"] is True
    assert tech["url_structure"]["parameter_urls_count"] >= 1
    assert tech["url_structure"]["slug_issues_count"] >= 1
    assert tech["indexability"]["indexable_count"] == 2
    assert tech["indexability"]["breakdown"]["noindex"] == 1
    assert tech["indexability"]["breakdown"]["client_error_4xx"] == 1
    assert tech["indexability"]["breakdown"]["robots_blocked"] == 1
    assert tech["content_distribution"]["word_count_buckets"]["comprehensive_800_1500"] == 1
    assert tech["content_distribution"]["word_count_buckets"]["thin_lt_300"] == 1
    assert len(tech["domain_history"]) >= 2

    records = db.scalars(select(DomainAnalysisRecord).where(DomainAnalysisRecord.domain == "example.com.tr")).all()
    assert len(records) >= 2
