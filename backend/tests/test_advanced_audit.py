import asyncio

import pytest

from app.models import CrawlRun, Site
from app.services import advanced_audit as aa
from app.services.crawler.parser import hamming, parse_html, simhash
from app.worker import run_once
from tests.conftest import html

ARTICLE = " ".join(f"kelime{i % 40} cümle{i % 13} paragraf{i % 7}" for i in range(120))


def routes(phase):
    noindex = '<meta name="robots" content="noindex">' if phase == 2 else ""
    product_ld = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Makine","image":"x.jpg"}</script>'
    nav = '<nav><a href="/">Home</a><a href="/urun/1">P1</a><a href="/urun/2">P2</a><a href="/blog/a">A</a><a href="/blog/b">B</a></nav>'
    r = {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow:\n" if phase == 1 else "User-agent: *\nDisallow: /\n"),
        "/": html("Kahve Makineleri Mağazası", nav + '<h1>Kahve makineleri</h1><img src="a.jpg">' + "".join(f'<a href="/liste?sort={i}&color={i % 3}">L{i}</a>' for i in range(1, 22)) + f"<p>{ARTICLE}</p>"),
        "/urun/1": html("Espresso Makinesi Pro", nav + f"<h1>Espresso makinesi pro</h1><span class='price'>1.299 TL</span><p>{ARTICLE}</p>",
                        product_ld + noindex),
        "/urun/2": html("Filtre Kahve Makinesi", nav + f"<h1>Filtre kahve makinesi</h1><span class='price'>899 TL</span><p>{ARTICLE} ek</p>"),
        "/blog/a": html("Espresso makinesi nasıl seçilir", nav + f"<h1>Espresso makinesi seçimi</h1><p>{ARTICLE}</p>"),
        "/blog/b": html("Kahve çekirdeği rehberi", nav + "<h1>Kahve çekirdeği</h1><p>" + " ".join(f"farklı{i} içerik{i}" for i in range(150)) + "</p>"),
    }
    for i in range(1, 22):
        r[f"/liste?sort={i}&color={i % 3}"] = html(f"Liste {i}", f"<p>{ARTICLE}</p>")
    return r


def test_parser_structured_data_simhash_template():
    p = parse_html(b'<html><body><script type="application/ld+json">{"@graph":[{"@type":"Organization","name":"X"},'
                   b'{"@type":"BreadcrumbList"}]}</script><script type="application/ld+json">{bad</script>'
                   b'<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">a</span></div>'
                   b'<img src="1.jpg"><img src="2.jpg" alt="ok"><p>' + ARTICLE.encode() + b'</p></body></html>', "https://e.com/")
    types = {(i["format"], i.get("type")) for i in p.structured_data}
    assert {("json-ld", "Organization"), ("json-ld", "BreadcrumbList"), ("json-ld", None), ("microdata", "Product")} <= types
    assert p.images_missing_alt == 1 and p.template_sig and p.simhash is not None
    natural = " ".join(f"sözcük{i}" for i in range(400))
    a, b = simhash(natural), simhash(natural + " küçük ek")
    assert hamming(a, b) <= 3 and hamming(a, simhash(" ".join(f"tamamen{i} başka{i}" for i in range(200)))) > 10


def test_custom_extraction_limits_and_redos():
    ex = aa.validate_extractors([{"name": "price", "type": "css", "expr": ".price"},
                                 {"name": "sku", "type": "xpath", "expr": "//span[@class='price']/text()"},
                                 {"name": "evil", "type": "regex", "expr": "(a+)+$"}])
    out = aa.extract_custom(ex, b"<html><body><span class='price'>899 TL</span>" + b"a" * 5000 + b"!</body></html>")
    assert out["price"] == ["899 TL"] and out["sku"] == ["899 TL"] and out["evil"] == {"error": "regex timeout"}
    with pytest.raises(ValueError):
        aa.validate_extractors([{"name": "x", "type": "shell", "expr": "rm"}])
    with pytest.raises(Exception):
        aa.validate_extractors([{"name": "x", "type": "regex", "expr": "("}])


def test_pagerank_properties():
    class P:
        def __init__(self, u):
            self.normalized_url, self.status_code = u, 200
    pages = [P(u) for u in "abcd"]
    pr = aa.internal_pagerank(pages, [("a", "b"), ("c", "b"), ("d", "b"), ("b", "a")])
    assert pr["b"] == 100.0 and pr["b"] > pr["a"] > pr["c"] and pr["c"] == pr["d"]


def test_advanced_audit_end_to_end_with_regressions(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    fx = fixture_site(routes(1))
    site = Site(org_id=1, project_id=1, name="Shop", base_url=fx.base + "/", allowed_hosts=["127.0.0.1"], conversion_events={},
                crawl_settings={"max_pages": 60, "max_depth": 30}, money_page_patterns=["^/urun/"],
                custom_extractors=[{"name": "price", "type": "css", "expr": ".price"}])
    db.add(site)
    db.commit()
    c1 = client.post(f"/api/v1/sites/{site.id}/crawls").json()["id"]
    asyncio.run(run_once("w"))
    summary = client.get(f"/api/v1/crawls/{c1}/summary").json()
    rules = {r["rule_id"] for r in summary["findings_by_rule"]}
    assert {"structured_data_missing_required", "parameter_explosion", "images_missing_alt", "near_duplicate_content"} <= rules
    assert 0 <= summary["crawl"]["health_score"] <= 100
    p1 = next(p for p in client.get(f"/api/v1/crawls/{c1}/pages", params={"q": "/urun/2"}).json()["items"])
    detail = client.get(f"/api/v1/pages/{p1['id']}").json()
    assert detail["findings_count"] >= 0
    from app.models import CrawlPage
    page = db.get(CrawlPage, p1["id"])
    assert page.extracted == {"price": ["899 TL"]} and page.internal_pagerank is not None
    assert client.get(f"/api/v1/crawls/{c1}/templates").status_code == 200
    sugg = client.get(f"/api/v1/crawls/{c1}/link-suggestions").json()
    assert "method" in sugg
    assert client.get(f"/api/v1/crawls/{c1}/diff").json()["available"] is False
    # reanalysis stays idempotent with advanced findings
    before = summary["total_findings"]
    client.post(f"/api/v1/crawls/{c1}/reanalyze")
    assert client.get(f"/api/v1/crawls/{c1}/summary").json()["total_findings"] == before
    # second crawl: money page gets noindex, robots.txt becomes "Disallow: /"
    fx.routes.clear()
    fx.routes.update(routes(2))
    db.get(CrawlRun, c1)
    c2 = client.post(f"/api/v1/sites/{site.id}/crawls").json()["id"]
    asyncio.run(run_once("w"))
    diff = client.get(f"/api/v1/crawls/{c2}/diff").json()
    kinds = {a["type"] for a in diff["regression_alerts"]}
    assert "robots_disallow_all" in kinds
    assert diff["previous_crawl"] == c1 and diff["current_crawl"] == c2


def test_site_update_validates_patterns(client, db):
    s = client.post("/api/v1/sites", json={"name": "x", "base_url": "https://x.example"}).json()
    assert client.patch(f"/api/v1/sites/{s['id']}", json={"money_page_patterns": ["("]}).status_code == 422
    assert client.patch(f"/api/v1/sites/{s['id']}", json={"custom_extractors": [{"name": "a", "type": "css", "expr": "!!"}]}).status_code == 422
    r = client.patch(f"/api/v1/sites/{s['id']}", json={"money_page_patterns": ["^/checkout"]}).json()
    assert r["money_page_patterns"] == ["^/checkout"] and r["verified"] is False


def test_crawl_diff_regression_rules(db):
    from app.models import CrawlPage
    s = Site(org_id=1, project_id=1, name="s", base_url="https://shop.example/", allowed_hosts=["shop.example"],
             conversion_events={}, crawl_settings={}, money_page_patterns=["^/urun/"])
    db.add(s); db.flush()
    r1 = CrawlRun(site_id=s.id, status="completed", config={}, sitemap_urls=["https://shop.example/a", "https://shop.example/b"])
    r2 = CrawlRun(site_id=s.id, status="completed", config={}, sitemap_urls=["https://shop.example/a"])
    db.add_all([r1, r2]); db.flush()
    rows = [(r1, "/urun/1", 200, None, None), (r2, "/urun/1", 200, "noindex", None),
            (r1, "/urun/2", 200, None, None), (r2, "/urun/2", 503, None, None),
            (r1, "/blog", 200, None, "https://shop.example/blog"), (r2, "/blog", 200, None, "https://other.example/blog")]
    for run, path, code, robots, canon in rows:
        u = "https://shop.example" + path
        db.add(CrawlPage(crawl_run_id=run.id, url=u, normalized_url=u, status_code=code, meta_robots=robots, canonical_url=canon, depth=0))
    db.commit()
    d = aa.crawl_diff(db, r2.id)
    by = {a["type"]: a for a in d["regression_alerts"]}
    assert by["money_page_noindex"]["severity"] == "critical" and by["money_page_noindex"]["url"].endswith("/urun/1")
    assert by["became_5xx"]["severity"] == "critical"  # money page
    assert by["canonical_other_host"]["details"]["to"] == "https://other.example/blog"
    assert by["sitemap_urls_dropped"]["details"]["sample"] == ["https://shop.example/b"]
    assert {"url": "https://shop.example/urun/2", "from": 200, "to": 503} in d["status_changes"]


def test_module4_audit_report_finding_enrichment_and_status_lifecycle(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    fx = fixture_site(routes(1))
    site = Site(
        org_id=1,
        project_id=1,
        name="Mod4Site",
        base_url=fx.base + "/",
        allowed_hosts=["127.0.0.1"],
        conversion_events={},
        crawl_settings={"max_pages": 40, "max_depth": 10},
    )
    db.add(site)
    db.commit()
    cid = client.post(f"/api/v1/sites/{site.id}/crawls").json()["id"]
    asyncio.run(run_once("w"))

    # 1. Verify findings have category, root_cause, seo_impact, priority, and status='open'
    findings = client.get(f"/api/v1/crawls/{cid}/findings").json()["items"]
    assert len(findings) >= 3
    f0 = findings[0]
    assert f0["category"] and f0["root_cause"] and f0["seo_impact"] and f0["priority"] > 0
    assert f0["status"] == "open"

    # 2. Update finding status through open -> in_progress -> fixed and filter
    upd = client.patch(f"/api/v1/findings/{f0['id']}", json={"status": "in_progress"})
    assert upd.status_code == 200 and upd.json()["status"] == "in_progress"
    in_prog = client.get(f"/api/v1/crawls/{cid}/findings", params={"status": "in_progress"}).json()
    assert in_prog["total"] == 1 and in_prog["items"][0]["id"] == f0["id"]

    # 3. Reanalyze preserves status on matching fingerprint
    client.post(f"/api/v1/crawls/{cid}/reanalyze")
    in_prog_after = client.get(f"/api/v1/crawls/{cid}/findings", params={"status": "in_progress"}).json()
    assert in_prog_after["total"] == 1

    # 4. Full Audit Report endpoint
    rep = client.get(f"/api/v1/crawls/{cid}/audit-report").json()
    assert 0 <= rep["health_score"] <= 100
    assert rep["by_status"]["in_progress"] == 1
    assert len(rep["grouped_issues"]) >= 3
    assert all(g["root_cause"] and g["seo_impact"] and g["category"] for g in rep["grouped_issues"])
    assert rep["performance_summary"]["pagespeed_api_status"] in ("CONNECTED", "INTEGRATION_CREDENTIALS_MISSING")
    assert rep["provenance"]["data_source"] == "Internal_Crawler"
