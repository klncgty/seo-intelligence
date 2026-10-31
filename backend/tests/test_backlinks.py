import asyncio
import gzip
import io
import json

import pytest
from sqlalchemy import text

from app.infra.clickhouse import ClickHouse
from app.providers import registry, runtime
from app.services import backlinks as bl
from app.services import commoncrawl as cc
from tests.conftest import html


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
    return c


def L(src, tgt, anchor="", rel="", pos="content", **kw):
    return {"source_url": src, "target_url": tgt, "anchor": anchor, "rel": rel, "link_position": pos, **kw}


def test_anchor_classification_and_disavow():
    assert bl.classify_anchor("Örnek Mağaza", "ornekmagaza.com.tr") == "brand"
    assert bl.classify_anchor("https://ornek.com", "ornek.com") == "url"
    assert bl.classify_anchor("buraya", "x.com") == "generic"
    assert bl.classify_anchor("kahve makinesi", "x.com", ["kahve makinesi"]) == "exact"
    assert bl.classify_anchor("ucuz kahve fiyatları", "x.com", ["kahve makinesi"]) == "partial"
    assert bl.classify_anchor("", "x.com") == "image_or_empty"
    txt = bl.disavow_file(["www.spam.com", "spam.com", "*.xyz", "other.net"])
    assert txt.splitlines()[-2:] == ["domain:other.net", "domain:spam.com"] and "rarely" in txt


def test_index_analyses_strength_intersect_risky(ch):
    links = [L(f"https://blog{i}.com/p", "https://me.com.tr/", "Me", ) for i in range(3)]
    links += [L(f"https://blog{i}.com/p", "https://rival1.com/", "rival") for i in range(5)]
    links += [L(f"https://blog{i}.com/p", "https://rival2.com/x", "rival2") for i in range(3, 6)]
    links += [L("https://news.com/a", "https://blog0.com/", "blog")] * 1 + [L("https://news.com/b", "https://blog1.com/", "blog")]
    links += [L("https://farm.biz/p", f"https://t{i}.com/", "x") for i in range(60)] + [L("https://farm.biz/p", "https://me.com.tr/", "kahve")]
    links += [L(f"https://partner.com/p{i}", "https://me.com.tr/", "Me", pos="footer") for i in range(25)]
    assert bl.ingest(ch, links, "own_crawl", "OBSERVED") == len(links)
    assert bl.ingest(ch, [L("https://same.com/a", "https://same.com/b")], "own_crawl", "OBSERVED") == 0  # internal skipped
    ov = bl.overview(ch, "me.com.tr")
    assert ov["referring_domains"] == 5 and ov["coverage"]["statement"].endswith("may be higher.")
    assert ov["link_positions"]["footer"] == 25 and ov["anchor_distribution"]["brand"] == 28
    res = bl.compute_domain_strength(ch)
    assert res["domains"] > 60 and res["model_version"] == "ds-1"
    s = bl.strengths(ch, ["blog0.com", "t5.com", "rival1.com"])
    assert s["blog0.com"] > s["t5.com"]  # linked by news.com, which links nowhere else
    assert s["rival1.com"] > s["t5.com"]  # 5 RDs vs 1 link-farm RD
    pros = bl.intersect(ch, "me.com.tr", ["rival1.com", "rival2.com"])
    assert {p["domain"] for p in pros["prospects"]} == {"blog3.com", "blog4.com"}  # link to both rivals, not to me
    risky = bl.risky_links(ch, "me.com.tr")
    kinds = {(r["signal"], r["domain"]) for r in risky["signals"]}
    assert ("link_farm_pattern", "farm.biz") in kinds and ("sitewide_footer_links", "partner.com") in kinds
    assert "rarely needed" in risky["warning"]


def _wat_member(src, links, date="2026-08-15T00:00:00Z"):
    payload = json.dumps({"Envelope": {"WARC-Header-Metadata": {"WARC-Target-URI": src, "WARC-Date": date},
                                       "Payload-Metadata": {"HTTP-Response-Metadata": {"HTML-Metadata": {"Links": links}}}}}).encode()
    rec = b"WARC/1.0\r\nWARC-Type: metadata\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload + b"\r\n\r\n"
    return gzip.compress(rec)


def test_common_crawl_wat_import_filter_and_resume(ch):
    members = [_wat_member("https://haber.com.tr/a", [{"path": "A@/href", "url": "https://me.com.tr/urun", "text": "ürün"},
                                                       {"path": "A@/href", "url": "/ic-link", "text": "iç"},
                                                       {"path": "A@/href", "url": "https://other.com/", "text": "x"}]),
               _wat_member("https://blog.org/b", [{"path": "IMG@/src", "url": "https://cdn.me.com.tr/i.png", "alt": "logo"},
                                                  {"path": "A@/href", "url": "https://site.tr/", "text": "tr site"}]),
               _wat_member("https://x.net/c", [{"path": "A@/href", "url": "https://me.com.tr/z", "text": "z"}])]
    blob = b"".join(members)
    chunks = [blob[i:i + 7] for i in range(0, len(blob), 7)]  # tiny chunks: members span many chunks
    states = []
    first = cc.import_stream(ch, chunks, {"me.com.tr", ".tr"}, max_records=2, checkpoint=states.append, batch=1)
    assert first["records"] == 2 and first["links"] == 3 and first["offset"] == len(members[0]) + len(members[1])
    rest = cc.import_stream(ch, [blob[first["offset"]:]], {"me.com.tr", ".tr"}, start_offset=first["offset"])
    assert rest["records"] == 1 and rest["links"] == 1 and rest["offset"] == len(blob)
    rows = ch.query("SELECT target_url, anchor, is_image, source FROM backlinks FINAL ORDER BY target_url")
    assert [r["target_url"] for r in rows] == ["https://cdn.me.com.tr/i.png", "https://me.com.tr/urun", "https://me.com.tr/z", "https://site.tr/"]
    assert all(r["source"] == "common_crawl" for r in rows)
    with pytest.raises(ValueError):
        next(cc.http_chunks("https://evil.example/file.wat.gz"))


def test_verify_links_and_reclamation(client, db, ch, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    me = fixture_site({"/": html("Home", '<a href="/yeni">n</a><a href="/eski">old</a>'), "/yeni": html("Yeni"),
                       "/eski": (404, {"Content-Type": "text/html"}, "gone")})
    ref = fixture_site({"/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /private\n"),
                        "/post": html("Post", f'<a href="{me.base}/eski">still links</a>'), "/gone-post": html("P", "no link"),
                        "/private/x": html("secret", f'<a href="{me.base}/">x</a>')})
    from app.models import Site
    site = Site(org_id=1, project_id=1, name="me", base_url=me.base + "/", allowed_hosts=["127.0.0.1"], conversion_events={}, crawl_settings={})
    db.add(site); db.commit()
    # ref site is "localhost" so registrable domains differ from 127.0.0.1
    rb = ref.base.replace("127.0.0.1", "localhost")
    old = "2026-01-01"
    bl.ingest(ch, [L(f"{rb}/post", f"{me.base}/eski", "eski", last_checked=old), L(f"{rb}/gone-post", f"{me.base}/", "home", last_checked=old),
                   L(f"{rb}/private/x", f"{me.base}/", "p", last_checked=old)], "provider:test", "PROVIDER")
    res = asyncio.run(bl.verify_links(ch, "127.0.0.1", stale_days=1))
    assert res == {"checked_sources": 3, "live": 1, "lost": 1, "skipped_robots": 1}
    assert "/private/x" not in ref.hits
    client.post(f"/api/v1/sites/{site.id}/crawls")
    from app.worker import run_once
    asyncio.run(run_once("w"))
    rec = client.post(f"/api/v1/backlinks/sites/{site.id}/reclamation").json()
    assert rec["created"] == 1
    opp = client.get(f"/api/v1/sites/{site.id}/opportunities", params={"type": "broken_backlink_reclamation"}).json()["items"][0]
    assert opp["url"].endswith("/eski") and opp["evidence"]["status_code"] == 404 and "partial" in opp["limitations"]


def test_provider_backlinks_demo_and_kd_authority(client, db, ch):
    assert client.post("/api/v1/backlinks/me.com.tr/fetch", json={"confirm": True}).json()["error"]["code"] == "provider_not_configured"
    db.execute(text("UPDATE organizations SET is_demo = true WHERE id = 1")); db.commit()
    r = client.post("/api/v1/backlinks/me.com.tr/fetch", json={"confirm": True, "limit": 8}).json()
    assert r["ingested"] == 8
    row = ch.query("SELECT source_class, is_synthetic, source FROM backlinks FINAL LIMIT 1")[0]
    assert row["source_class"] == "DEMO" and int(row["is_synthetic"]) == 1 and row["source"] == "provider:mock"
    # KD gains the authority component once all top-10 domains have a strength score
    from app.services.keywords import models
    full = models.keyword_difficulty(["a.com", "b.com"], [], {"a.com": 50, "b.com": 70})
    assert full["missing_inputs"] == [] and full["components"]["authority"] == 60


def test_module6_backlink_analytics_7_criteria_audit_and_disavow(client, ch):
    links = [
        L("https://casino-bet.xyz/post1", "https://target.com.tr/urun", "kahve makinesi", rel="follow", pos="footer"),
        L("https://casino-bet.xyz/post2", "https://target.com.tr/urun", "kahve makinesi", rel="follow", pos="footer"),
        L("https://casino-bet.xyz/post3", "https://target.com.tr/urun", "kahve makinesi", rel="follow", pos="footer"),
        L("https://dead-blog.com/old", "https://target.com.tr/", "Target", status="lost"),
        L("https://trusted.com.tr/review", "https://target.com.tr/urun", "Target Mağaza", rel="ugc", is_image=True),
    ]
    bl.ingest(ch, links, "own_crawl", "OBSERVED")

    ov = client.get("/api/v1/backlinks/target.com.tr/overview", params={"keywords": ["kahve makinesi"]}).json()
    assert ov["total_backlinks"] == 5
    assert ov["live_links"] == 4 and ov["lost_links"] == 1
    assert ov["ugc_links"] == 1
    assert ov["anchor_types"]["exact_match"] == 3
    assert ov["link_types"]["image"] == 1
    assert ov["country_distribution"]
    assert ov["provenance"]["dataSource"]

    audit = client.get("/api/v1/backlinks/target.com.tr/audit", params={"keywords": ["kahve makinesi"]}).json()
    assert audit["summary"]["total_referring_domains_audited"] == 3
    toxic = next(a for a in audit["audited_domains"] if a["source_domain"] == "casino-bet.xyz")
    assert toxic["risk_score"] >= 60
    assert toxic["risk_level"] == "high"
    assert toxic["recommended_action"] == "disavow_candidate"
    assert "low_quality_or_spammy_domain" in toxic["signals"]
    assert "irrelevant_or_toxic_niche" in toxic["signals"]
    assert "unnatural_anchor_text" in toxic["signals"]

    lost_dom = next(a for a in audit["audited_domains"] if a["source_domain"] == "dead-blog.com")
    assert "unreachable_or_lost_source" in lost_dom["signals"]

    disavow_res = client.post(
        "/api/v1/backlinks/disavow",
        json={"domains": ["casino-bet.xyz"], "urls": ["https://spam.example.com/bad-link"]},
    )
    assert disavow_res.status_code == 200
    assert "https://spam.example.com/bad-link" in disavow_res.text
    assert "domain:casino-bet.xyz" in disavow_res.text


def test_module8_link_building_prospects_contact_and_outreach_crm(client, db, ch, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    pub_site = fixture_site({
        "/rehber": html(
            "Kahve Kaynakları",
            '<p>İletişim için <a href="mailto:editor@kahverehberi.org">editor@kahverehberi.org</a> '
            'veya <a href="/iletisim">İletişim</a> ve <a href="https://linkedin.com/company/kahverehberi">LinkedIn</a></p>',
        )
    })
    site = client.post("/api/v1/sites", json={"name": "Kahve Dünyası", "base_url": "https://kahvedunyasi.com.tr"}).json()
    site_id = site["id"]
    client.post(f"/api/v1/sites/{site_id}/competitors", json={"name": "Rakip 1", "domain": "rakip1.com.tr"})
    client.post(f"/api/v1/sites/{site_id}/competitors", json={"name": "Rakip 2", "domain": "rakip2.com.tr"})

    bl.ingest(ch, [
        L("https://dergi.org.tr/kahve", "https://rakip1.com.tr/", "Rakip 1"),
        L("https://dergi.org.tr/en-iyi", "https://rakip2.com.tr/", "Rakip 2"),
        L("https://eski-blog.com/post", "https://kahvedunyasi.com.tr/404", "Eski Link", status="lost"),
    ], "own_crawl", "OBSERVED")

    disc = client.post(f"/api/v1/sites/{site_id}/link-prospects/discover").json()
    assert disc["total"] >= 2
    types = {p["link_type"] for p in disc["items"]}
    assert "link_gap" in types and "broken_link" in types

    manual = client.post(
        f"/api/v1/sites/{site_id}/link-prospects",
        json={"source_url": f"{pub_site.base}/rehber", "link_type": "resource_page", "relevance_score": 0.9, "domain_strength": 65.0},
    )
    assert manual.status_code == 201
    pid = manual.json()["id"]

    scanned = client.post(f"/api/v1/link-prospects/{pid}/scan-contact").json()
    assert scanned["contact_info"]["status"] == "PUBLIC_CONTACT_FOUND"
    assert "editor@kahverehberi.org" in scanned["contact_info"]["emails"]
    assert scanned["contact_info"]["fabricated"] is False

    drafted = client.post(f"/api/v1/link-prospects/{pid}/draft").json()
    assert drafted["outreach_status"] == "REVIEWING"
    assert drafted["email_draft"]["requires_user_approval"] is True
    assert drafted["email_draft"]["auto_send_enabled"] is False

    updated = client.patch(
        f"/api/v1/link-prospects/{pid}",
        json={"outreach_status": "CONTACTED", "approve_draft": True, "note": "Editöre e-posta gönderildi"},
    ).json()
    assert updated["outreach_status"] == "CONTACTED"
    assert updated["email_draft"]["user_approved"] is True
    assert len(updated["outreach_history"]) == 1
