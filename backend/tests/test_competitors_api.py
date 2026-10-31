import asyncio
from datetime import date, timedelta

from sqlalchemy import func, select

from app.models import ExternalSeoSnapshot, SearchPerformanceDaily, Site
from app.worker import run_once
from tests.conftest import html

WORDS = " ".join(["content"] * 200)


def own_routes():
    return {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n"),
        "/": html("Acme Home", f'<h1>Acme</h1><a href="/blog/running-shoes-guide">Guide</a><p>{WORDS}</p>'),
        "/blog/running-shoes-guide": html("Running shoes guide", f"<h1>Running shoes guide</h1><p>{WORDS}</p>"),
    }


def comp_routes():
    links = ('<a href="/pricing">Pricing</a><a href="/blog/trail-running-tips">Trail</a><a href="/blog/trail-running-gear">Gear</a>'
             '<a href="/login">Login</a><a href="/account/orders">Orders</a><a href="/cart">Cart</a>'
             '<a href="/acme-vs-rival">Vs</a><a href="/rival-vs-other">Vs2</a><a href="https://evil.example/">x</a>')
    return {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /internal\n"),
        "/": (200, {"Content-Type": "text/html", "Set-Cookie": "session=abc; Path=/"},
              f"<html><head><title>Rival</title></head><body><h1>Rival</h1>{links}<p>{WORDS}</p></body></html>"),
        "/pricing": html("Pricing plans", f"<h1>Pricing</h1><p>{WORDS}</p>"),
        "/blog/trail-running-tips": html("Trail running tips", f"<h1>Trail running tips</h1><p>{WORDS}</p>"),
        "/blog/trail-running-gear": html("Trail running gear", f"<h1>Trail running gear</h1><p>{WORDS}</p>"),
        "/acme-vs-rival": html("Acme vs Rival comparison", f"<h1>Acme vs Rival</h1><p>{WORDS}</p>"),
        "/rival-vs-other": html("Rival vs Other comparison", f"<h1>Rival vs Other</h1><p>{WORDS}</p>"),
        "/login": html("Login"), "/account/orders": html("Orders"), "/cart": html("Cart"),
    }


def test_competitor_crud(client, db):
    site = client.post("/api/v1/sites", json={"name": "Acme", "base_url": "https://acme.example"}).json()
    base = f"/api/v1/sites/{site['id']}/competitors"
    r = client.post(base, json={"name": "Rival", "domain": "www.rival.example", "notes": "main rival"})
    assert r.status_code == 201
    c = r.json()
    assert c["domain"] == "rival.example" and set(c["allowed_hosts"]) == {"rival.example", "www.rival.example"}
    assert client.post(base, json={"name": "Dup", "domain": "rival.example"}).status_code == 409
    assert client.post(base, json={"name": "Self", "domain": "acme.example"}).status_code == 422
    for bad in ["169.254.169.254", "localhost", "10.0.0.1", "javascript:alert(1)"]:
        assert client.post(base, json={"name": "Bad", "domain": bad}).status_code == 422, bad
    r = client.patch(f"{base}/{c['id']}", json={"notes": "updated", "domain": "rival2.example"})
    assert r.json()["notes"] == "updated" and r.json()["domain"] == "rival2.example"
    assert client.get(base).json()["total"] == 1
    other = client.post("/api/v1/sites", json={"name": "Other", "base_url": "https://other.example"}).json()
    assert client.get(f"/api/v1/sites/{other['id']}/competitors/{c['id']}").status_code == 404  # scoped to site
    assert client.delete(f"{base}/{c['id']}").status_code == 204 and client.get(base).json()["total"] == 0


def test_competitor_crawl_compare_gaps_and_separation(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    own, comp = fixture_site(own_routes()), fixture_site(comp_routes())
    site = Site(org_id=1, project_id=1, name="Acme", base_url=own.base + "/", allowed_hosts=["127.0.0.1"], conversion_events={}, crawl_settings={})
    db.add(site)
    db.commit()
    # own GSC: demand for "trail running" exists in first-party data
    d = date.today() - timedelta(days=10)
    db.add(SearchPerformanceDaily(site_id=site.id, date=d, page=own.base + "/", query="trail running shoes",
                                  device="MOBILE", country="usa", clicks=3, impressions=400, ctr=0.0075, position=14.0))
    db.commit()
    base = f"/api/v1/sites/{site.id}/competitors"
    unavailable = client.get(f"/api/v1/sites/{site.id}/competitors-comparison").json()
    assert unavailable["available"] is False
    c = client.post(base, json={"name": "Rival", "domain": f"http://localhost:{comp.port}/"}).json()
    client.post(f"/api/v1/sites/{site.id}/crawls")
    cr = client.post(f"{base}/{c['id']}/crawls").json()
    assert cr["competitor_id"] == c["id"] and cr["config"]["max_pages"] <= 300 and cr["config"]["concurrency"] <= 2
    assert client.post(f"{base}/{c['id']}/crawls").status_code == 409
    while asyncio.run(run_once("w")):
        pass
    # private/account areas and external domains never requested; cookies never sent back
    assert not {"/login", "/account/orders", "/cart"} & set(comp.hits)
    assert all("cookie" not in h for h in comp.request_headers)
    assert "/pricing" in comp.hits
    # own crawl list excludes competitor crawls
    assert client.get(f"/api/v1/sites/{site.id}/crawls").json()["total"] == 1
    cmp = client.get(f"/api/v1/sites/{site.id}/competitors-comparison").json()
    assert cmp["available"] is True and {s["label"] for s in cmp["sources"]} == {"own", "Rival"}
    assert all(s["updated_at"] for s in cmp["sources"])
    assert any(m["page_type"] == "comparison" for m in cmp["missing_page_types"])
    assert cmp["caveats"]
    gaps = client.get(f"/api/v1/sites/{site.id}/competitors-content-gaps").json()
    assert gaps["available"] and gaps["own_gsc"]["source"].startswith("gsc:own")
    trail = next(g for g in gaps["gaps"] if g["topic"] in ("trail running", "trail"))
    assert trail["evidence_level"] == "own_demand" and trail["own_gsc_evidence"]["impressions"] == 400
    # opportunities: evidence-based, unknown impact, explicit limitation
    gen = client.post(f"/api/v1/sites/{site.id}/competitors-opportunities/generate").json()
    assert gen["available"] and gen["total"] > 0
    opps = client.get(f"/api/v1/sites/{site.id}/opportunities", params={"page_size": 200}).json()["items"]
    comp_opps = [o for o in opps if o["opportunity_type"].startswith("competitor_")]
    assert comp_opps and len(comp_opps) == gen["total"]
    for o in comp_opps:
        assert "does not prove it ranks better" in o["limitations"]
        assert o["impact_estimate"]["value"] is None and o["data_sources"] and o["recommended_action"]
    gap_opp = next(o for o in comp_opps if o["opportunity_type"] == "competitor_content_gap")
    assert any(s.startswith("gsc:own") for s in gap_opp["data_sources"])
    again = client.post(f"/api/v1/sites/{site.id}/competitors-opportunities/generate").json()
    assert again["created"] == 0  # idempotent
    # first-party GSC table untouched by competitor work
    assert db.scalar(select(func.count()).select_from(SearchPerformanceDaily)) == 1


def test_external_provider_unavailable_without_keys(client, db):
    st = client.get("/api/v1/seo-data/status").json()
    assert st["configured"] is False and st["features"]["keyword_gap"] is False and st["note"]
    site = client.post("/api/v1/sites", json={"name": "Acme", "base_url": "https://acme.example"}).json()
    client.post(f"/api/v1/sites/{site['id']}/competitors", json={"name": "Rival", "domain": "rival.example"})
    kg = client.post(f"/api/v1/sites/{site['id']}/competitors-keyword-gap").json()
    bl = client.post(f"/api/v1/sites/{site['id']}/competitors-backlinks").json()
    assert kg["available"] is False and "data" not in kg and bl["available"] is False and "comparison" not in bl
    assert db.scalar(select(func.count()).select_from(ExternalSeoSnapshot)) == 0
    assert client.get(f"/api/v1/sites/{site['id']}/external-seo-data").json()["items"] == []


def test_suggestions_are_not_auto_added(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    own = fixture_site({"/": html("Home", '<a href="https://rival.example/">r</a><a href="https://rival.example/x">r</a>'
                                          '<a href="https://twitter.com/acme">t</a>')})
    site = Site(org_id=1, project_id=1, name="Acme", base_url=own.base + "/", allowed_hosts=["127.0.0.1"], conversion_events={}, crawl_settings={})
    db.add(site)
    db.commit()
    client.post(f"/api/v1/sites/{site.id}/crawls")
    asyncio.run(run_once("w"))
    sug = client.get(f"/api/v1/sites/{site.id}/competitors/suggestions").json()
    assert sug["suggestions"][0] == {"domain": "rival.example", "outbound_links": 2} and sug["note"]
    assert "serp_competitors" in sug
    assert client.get(f"/api/v1/sites/{site.id}/competitors").json()["total"] == 0


def test_module5_competitor_traffic_and_multi_competitor_matrix(client, db, settings):
    import pytest
    from app.infra.clickhouse import ClickHouse
    from app.models import PpcKeywordRecord
    from app.services import backlinks as bl_service

    ch = ClickHouse(url="http://127.0.0.1:8123", database="seo_test")
    if not ch.ping():
        pytest.skip("ClickHouse not running")
    ch.command("DROP DATABASE IF EXISTS seo_test", database="default")
    ch.migrate()
    settings(clickhouse_url="http://127.0.0.1:8123", clickhouse_database="seo_test", minio_endpoint="")

    db_locale = "tr-TR:google.com.tr"
    today = date.today().isoformat()
    site = client.post("/api/v1/sites", json={"name": "Acme", "base_url": "https://acme.com"}).json()
    site_id = site["id"]
    for idx, dom in enumerate(("rival1.com", "rival2.com", "rival3.com"), start=1):
        r = client.post(f"/api/v1/sites/{site_id}/competitors", json={"name": f"Rival {idx}", "domain": dom})
        assert r.status_code == 201

    ch.insert("keyword_latest", [
        {"locale": db_locale, "keyword_key": "kosu ayakkabi", "keyword": "koşu ayakkabısı", "search_volume": 10000, "cpc": 1.5, "cpc_currency": "USD", "kd": 42.0, "intents": ["transactional"], "provider": "test", "source_class": "PROVIDER", "fetched_at": "2026-10-30 12:00:00", "version": 1},
        {"locale": db_locale, "keyword_key": "patika kosu", "keyword": "patika koşu ayakkabısı", "search_volume": 4000, "cpc": 1.2, "cpc_currency": "USD", "kd": 35.0, "intents": ["commercial"], "provider": "test", "source_class": "PROVIDER", "fetched_at": "2026-10-30 12:00:00", "version": 1},
        {"locale": db_locale, "keyword_key": "maraton antrenman", "keyword": "maraton antrenman", "search_volume": 2500, "cpc": 0.8, "cpc_currency": "USD", "kd": 20.0, "intents": ["informational"], "provider": "test", "source_class": "PROVIDER", "fetched_at": "2026-10-30 12:00:00", "version": 1},
    ])

    ch.insert("domain_keyword_rankings", [
        {"registrable_domain": "rival1.com", "locale": db_locale, "device": "desktop", "keyword_key": "kosu ayakkabi", "snapshot_date": today, "best_rank": 1, "url": "https://rival1.com/kosu", "version": 1},
        {"registrable_domain": "rival2.com", "locale": db_locale, "device": "desktop", "keyword_key": "kosu ayakkabi", "snapshot_date": today, "best_rank": 2, "url": "https://rival2.com/kosu", "version": 1},
        {"registrable_domain": "acme.com", "locale": db_locale, "device": "desktop", "keyword_key": "kosu ayakkabi", "snapshot_date": today, "best_rank": 8, "url": "https://acme.com/kosu", "version": 1},
        {"registrable_domain": "rival1.com", "locale": db_locale, "device": "desktop", "keyword_key": "patika kosu", "snapshot_date": today, "best_rank": 1, "url": "https://rival1.com/patika", "version": 1},
        {"registrable_domain": "rival2.com", "locale": db_locale, "device": "desktop", "keyword_key": "patika kosu", "snapshot_date": today, "best_rank": 2, "url": "https://rival2.com/patika", "version": 1},
        {"registrable_domain": "rival3.com", "locale": db_locale, "device": "desktop", "keyword_key": "patika kosu", "snapshot_date": today, "best_rank": 3, "url": "https://rival3.com/patika", "version": 1},
        {"registrable_domain": "acme.com", "locale": db_locale, "device": "desktop", "keyword_key": "maraton antrenman", "snapshot_date": today, "best_rank": 1, "url": "https://acme.com/maraton", "version": 1},
    ])

    bl_service.ingest(ch, [
        {"source_url": "https://news.example.com/a", "target_url": "https://rival1.com/", "anchor": "Rival 1", "rel": "follow", "link_position": "content", "is_image": False},
        {"source_url": "https://news.example.com/b", "target_url": "https://rival2.com/", "anchor": "Rival 2", "rel": "follow", "link_position": "content", "is_image": False},
    ], "own_crawl", "OBSERVED")

    db.add(PpcKeywordRecord(
        org_id=1,
        site_id=site_id,
        domain="rival1.com",
        keyword="koşu ayakkabısı",
        keyword_key="kosu ayakkabi",
        database=db_locale,
        position=1,
        search_volume=10000,
        estimated_cpc=1.5,
        competition=0.7,
        ad_headline="En İyi Koşu Ayakkabısı",
        ad_description="Ücretsiz kargo",
        data_source="SERP_Ads_Extractor",
        source_class="OBSERVED",
        is_estimated=True,
        confidence_score=0.75,
    ))
    db.commit()

    traffic = client.get(f"/api/v1/sites/{site_id}/competitors-traffic").json()
    assert len(traffic["domains"]) == 4  # own + 3 competitors
    r1_traffic = next(d for d in traffic["domains"] if d["domain"] == "rival1.com")
    assert r1_traffic["estimated_monthly_organic_traffic"] > 0
    assert r1_traffic["paid_keywords_count"] == 1
    assert r1_traffic["traffic_channels"]["direct"]["status"] == "INTEGRATION_CREDENTIALS_MISSING"
    assert r1_traffic["provenance"]["isEstimated"] is True

    matrix = client.get(f"/api/v1/sites/{site_id}/competitors-matrix").json()
    assert matrix["supports_multi_competitor"] is True
    assert matrix["competitor_count"] == 3
    assert len(matrix["matrix"]) == 4
    assert matrix["keyword_gap_summary"]["available"] is True
    assert matrix["keyword_gap_summary"]["counts"]["missing"] >= 1
    assert matrix["keyword_gap_summary"]["counts"]["weak"] >= 1
    assert matrix["keyword_gap_summary"]["counts"]["unique"] >= 1
    assert matrix["backlink_gap_summary"]["available"] is True
    assert matrix["backlink_gap_summary"]["total_prospects"] >= 1
