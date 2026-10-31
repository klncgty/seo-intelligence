"""Unit and Integration Tests for Enterprise Modules (Modules 9, 10, 11, 12)."""

from datetime import date
import pytest
from app.infra.clickhouse import ClickHouse


@pytest.fixture()
def ch(settings, db):
    c = ClickHouse(url="http://127.0.0.1:8123", database="seo_test")
    if not c.ping():
        pytest.skip("ClickHouse not running")
    c.command("DROP DATABASE IF EXISTS seo_test", database="default")
    c.migrate()
    settings(clickhouse_url="http://127.0.0.1:8123", clickhouse_database="seo_test", minio_endpoint="")
    return c


def test_module9_ppc_intelligence_and_hybrid_strategy(client, db, ch):
    db_locale = "tr-TR:google.com.tr"
    today = date.today().isoformat()

    site = client.post("/api/v1/sites", json={"name": "KahvePro", "base_url": "https://kahvepro.com.tr"}).json()
    site_id = site["id"]
    client.post(f"/api/v1/sites/{site_id}/competitors", json={"name": "BaristaShop", "domain": "baristashop.com.tr"})

    ch.insert("keyword_latest", [
        {"locale": db_locale, "keyword_key": "espresso makine", "keyword": "espresso makinesi", "search_volume": 12000, "cpc": 2.4, "cpc_currency": "USD", "kd": 62.0, "intents": ["transactional"], "provider": "test", "source_class": "PROVIDER", "fetched_at": "2026-10-30 12:00:00", "version": 1},
        {"locale": db_locale, "keyword_key": "kahve degirmen", "keyword": "kahve değirmeni", "search_volume": 6000, "cpc": 1.6, "cpc_currency": "USD", "kd": 28.0, "intents": ["commercial"], "provider": "test", "source_class": "PROVIDER", "fetched_at": "2026-10-30 12:00:00", "version": 1},
    ])

    ch.insert("domain_keyword_rankings", [
        {"registrable_domain": "kahvepro.com.tr", "locale": db_locale, "device": "desktop", "keyword_key": "kahve degirmen", "snapshot_date": today, "best_rank": 1, "url": "https://kahvepro.com.tr/degirmen", "version": 1},
        {"registrable_domain": "kahvepro.com.tr", "locale": db_locale, "device": "desktop", "keyword_key": "espresso makine", "snapshot_date": today, "best_rank": 14, "url": "https://kahvepro.com.tr/espresso", "version": 1},
    ])

    r1 = client.post(
        f"/api/v1/sites/{site_id}/ppc/keywords",
        json={
            "domain": "baristashop.com.tr",
            "keyword": "espresso makinesi",
            "database": db_locale,
            "position": 1,
            "search_volume": 12000,
            "estimated_cpc": 2.4,
            "competition": 0.85,
            "ad_headline": "BaristaShop Espresso Makineleri - %20 İndirim",
            "ad_description": "Aynı gün ücretsiz kargo ve 2 yıl garanti.",
            "landing_page": "https://baristashop.com.tr/kampanya/espresso",
        },
    )
    assert r1.status_code == 201

    ov = client.get(f"/api/v1/sites/{site_id}/ppc/overview", params={"database": db_locale}).json()
    assert ov["own_domain"] == "kahvepro.com.tr"
    comp_sum = next(d for d in ov["domain_summaries"] if d["domain"] == "baristashop.com.tr")
    assert comp_sum["paid_keywords_count"] == 1
    assert comp_sum["estimated_monthly_ad_spend_usd"] > 0
    assert len(comp_sum["ad_copies"]) == 1
    assert len(ov["paid_keyword_gap"]["missing_paid_keywords"]) == 1
    assert len(ov["hybrid_strategy"]["high_cpc_organic_targets"]) >= 1
    assert len(ov["hybrid_strategy"]["organic_saves_ppc_budget"]) >= 1
    assert len(ov["hybrid_strategy"]["paid_bridges_high_kd_gap"]) >= 1
    assert ov["provenance"]["isEstimated"] is True


def test_module10_social_media_and_approval_gated_publishing(client, db):
    site = client.post("/api/v1/sites", json={"name": "SocialBrand", "base_url": "https://socialbrand.com"}).json()
    site_id = site["id"]

    acc = client.post(
        f"/api/v1/sites/{site_id}/social/accounts",
        json={"platform": "x", "handle": "socialbrand_tr", "display_name": "SocialBrand TR"},
    ).json()
    assert acc["status"] == "credentials_missing"
    assert acc["requires_setup"] is True

    drafts = client.post(
        f"/api/v1/sites/{site_id}/social/generate-drafts",
        json={
            "source_url": "https://socialbrand.com/blog/seo-rehberi",
            "title": "2026 SEO Rehberi",
            "topic": "organik büyüme",
            "platforms": ["x", "linkedin"],
        },
    ).json()["items"]
    assert len(drafts) == 2
    x_draft = next(d for d in drafts if "x" in d["platforms"])
    assert "utm_source=x" in x_draft["content"]
    assert x_draft["status"] == "draft"
    assert x_draft["user_approved"] is False

    # Cannot publish without approval
    unapproved_pub = client.post(f"/api/v1/social/posts/{x_draft['id']}/publish")
    assert unapproved_pub.status_code == 400

    # User approval gate
    appr = client.post(f"/api/v1/social/posts/{x_draft['id']}/approve").json()
    assert appr["user_approved"] is True
    assert appr["status"] == "approved"

    # Attempt publish with unconfigured credentials -> honest missing credentials status
    pub_res = client.post(f"/api/v1/social/posts/{x_draft['id']}/publish").json()
    assert pub_res["status"] == "INTEGRATION_CREDENTIALS_MISSING"


def test_module11_ai_seo_diagnosis_and_mathematical_priority_kanban(client, db):
    from app.models import AuditFinding, CrawlRun
    site = client.post("/api/v1/sites", json={"name": "DiagSite", "base_url": "https://diagsite.com"}).json()
    site_id = site["id"]

    run = CrawlRun(site_id=site_id, status="completed", config={})
    db.add(run)
    db.flush()

    f1 = AuditFinding(
        crawl_run_id=run.id,
        rule_id="missing_title",
        fingerprint="fp1",
        url="https://diagsite.com/urun",
        severity="HIGH",
        title="Missing Title",
        description="Page lacks title tag",
        remediation="Add a descriptive title tag",
        category="Frontend",
        root_cause="Template missing head title element",
        seo_impact="Hurts CTR and search snippet",
    )
    f2 = AuditFinding(
        crawl_run_id=run.id,
        rule_id="http_error",
        fingerprint="fp2",
        url="https://diagsite.com/api/broken",
        severity="CRITICAL",
        title="500 Server Error",
        description="Internal server error on crawl",
        remediation="Fix unhandled server exception",
        category="Backend",
        root_cause="Unhandled database exception",
        seo_impact="Crawlers fail to access content",
    )
    db.add_all([f1, f2])
    db.commit()

    diag = client.post(f"/api/v1/sites/{site_id}/seo-diagnoses/run").json()
    assert diag["total"] == 2
    assert len(diag["kanban"]["new"]) == 2
    crit = next(d for d in diag["items"] if d["severity"] == "CRITICAL")
    assert crit["category"] == "Backend"
    assert crit["priority_score"] >= 65.0  # Mathematical priority engine

    # Update status to in_progress
    upd = client.patch(f"/api/v1/seo-diagnoses/{crit['id']}", json={"status": "in_progress"}).json()
    assert upd["status"] == "in_progress"


def test_module12_deep_code_analysis_and_approval_gated_fix_agent(client, db, tmp_path):
    site = client.post("/api/v1/sites", json={"name": "CodeSite", "base_url": "https://codesite.com"}).json()
    site_id = site["id"]

    # Create dummy static site repo
    repo_dir = tmp_path / "my_project"
    repo_dir.mkdir()
    index_file = repo_dir / "index.html"
    index_file.write_text("<!DOCTYPE html><html><head></head><body><h1>Hello</h1></body></html>")

    repo = client.post(
        f"/api/v1/sites/{site_id}/code-repositories",
        json={"name": "Frontend Repo", "repo_type": "local", "local_path": str(repo_dir)},
    ).json()
    assert repo["detected_framework"] == "static_html"

    # Scan and generate proposal
    scan = client.post(f"/api/v1/code-repositories/{repo['id']}/scan").json()
    assert len(scan["proposals"]) >= 1
    prop = scan["proposals"][0]
    assert prop["status"] == "proposed"
    assert prop["user_approved"] is False
    assert "<title>" in prop["unified_diff"]

    # Try apply without approval -> 400
    try_apply = client.post(f"/api/v1/code-fix-proposals/{prop['id']}/apply")
    assert try_apply.status_code == 400

    # Approve fix
    client.post(f"/api/v1/code-fix-proposals/{prop['id']}/approve", json={"user_approved": True})

    # Apply fix
    applied = client.post(f"/api/v1/code-fix-proposals/{prop['id']}/apply").json()
    assert applied["success"] is True
    assert applied["status"] == "approved_applied"
    assert "<title>" in index_file.read_text()

    # Rollback fix
    rb = client.post(f"/api/v1/code-fix-proposals/{prop['id']}/rollback").json()
    assert rb["success"] is True
    assert rb["status"] == "reverted"
    assert "<title>" not in index_file.read_text()

