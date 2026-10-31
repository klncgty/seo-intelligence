from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.infra.clickhouse import ClickHouse
from app.models import SearchPerformanceDaily, Site
from app.providers import registry, runtime
from app.services.keywords import models

DB = "tr-TR:google.com.tr"


# ---------------------------------------------------------------- pure models

def test_intent_rules_tr_en_multi():
    assert models.classify_intent("kahve makinesi fiyatları")["intents"][0] == "transactional"
    assert models.classify_intent("en iyi kahve makinesi")["intents"] == ["commercial"]
    assert models.classify_intent("kahve nasıl demlenir")["intents"] == ["informational"]
    assert models.classify_intent("best espresso machine", "en")["intents"] == ["commercial"]
    multi = models.classify_intent("en iyi kahve makinesi fiyat", serp_features=["shopping"])
    assert set(multi["intents"]) >= {"transactional", "commercial"} and "serp:shopping" in multi["evidence"]
    amb = models.classify_intent("kahve")
    assert amb["intents"] == [] and amb["ambiguous"]  # no guess without evidence


def test_kd_components_and_missing_authority():
    r = models.keyword_difficulty(["a.com", "b.com", "a.com"] + [f"d{i}.com" for i in range(7)], ["ai_overview", "people_also_ask"])
    assert r["components"]["authority"] is None and "authority" in r["missing_inputs"] and r["confidence"] == 0.4
    assert r["components"]["serp_crowding"] == 30.0 and r["kd"] is not None and r["model_version"] == "kd-1"
    full = models.keyword_difficulty(["a.com", "b.com"], [], {"a.com": 80, "b.com": 60})
    assert full["confidence"] == 1.0 and full["components"]["authority"] == 70
    assert models.keyword_difficulty([], [])["kd"] is None
    # stability: same input → same output
    assert models.keyword_difficulty(["a.com"], ["video"]) == models.keyword_difficulty(["a.com"], ["video"])


def test_serp_overlap_and_lexical_clustering():
    serps = {"kahve makinesi": ["u1", "u2", "u3", "u4"], "kahve makineleri": ["u1", "u2", "u3", "u9"],
             "espresso makinesi": ["u2", "u3", "u4", "u8"], "çay demliği": ["x1", "x2", "x3"]}
    cl = models.serp_overlap_clusters(serps, min_shared=3)
    groups = sorted(sorted(c.keywords) for c in cl)
    assert ["espresso makinesi", "kahve makineleri", "kahve makinesi"] in groups and ["çay demliği"] in groups
    assert len(models.serp_overlap_clusters(serps, min_shared=4)) == 4
    lex = models.lexical_clusters(["kahve makinesi", "kahve makineleri", "çay demliği"])
    assert any(set(c.keywords) == {"kahve makinesi", "kahve makineleri"} for c in lex)


def test_gsc_lower_bound_is_modeled():
    lb = models.gsc_volume_lower_bound([120, 300, 210])
    assert lb["value"] == 300 and lb["source_class"] == "MODELED" and lb["tenant_only"]
    assert models.gsc_volume_lower_bound([])["value"] is None


# ---------------------------------------------------------------- integration (ClickHouse + DEMO mock provider)

@pytest.fixture()
def ch(settings):
    c = ClickHouse(url="http://127.0.0.1:8123", database="seo_test")
    if not c.ping():
        pytest.skip("ClickHouse not running")
    c.command("DROP DATABASE IF EXISTS seo_test", database="default")
    c.migrate()
    settings(clickhouse_url="http://127.0.0.1:8123", clickhouse_database="seo_test", minio_endpoint="")
    registry.clear_overrides()
    runtime.BREAKER.failures.clear(); runtime.BREAKER.opened_at.clear(); runtime.BUCKET._local.clear()
    return c


@pytest.fixture()
def demo(db):
    db.execute(text("UPDATE organizations SET is_demo = true WHERE id = 1"))
    db.commit()


def test_provider_not_configured_and_analytics_unavailable(client, db, settings):
    settings(clickhouse_url="")
    assert client.get("/api/v1/keywords/explorer").json()["error"]["code"] == "analytics_unavailable"


def test_keyword_flow_lookup_serp_explorer_overview(client, db, ch, demo):
    kws = ["kahve makinesi", "Kahve Makinesi ", "kahve makinesi fiyatları", "espresso makinesi nasıl kullanılır"]
    r = client.post("/api/v1/keywords/lookup", json={"keywords": kws})
    assert r.status_code == 428 and r.json()["error"]["details"]["estimated_credits"] == pytest.approx(0.3)  # 3 unique
    r = client.post("/api/v1/keywords/lookup", json={"keywords": kws, "confirm": True, "request_id": "r1"})
    assert r.status_code == 200 and r.json()["ingested"] == 3
    again = client.post("/api/v1/keywords/lookup", json={"keywords": kws, "confirm": True, "request_id": "r2"}).json()
    assert again["cached"] is True and again["cost_usd"] == 0.0
    ex = client.get("/api/v1/keywords/explorer", params={"q": "kahve makinesi"}).json()
    assert ex["total"] == 2 and all(i["source_class"] == "DEMO" and int(i["is_synthetic"]) == 1 for i in ex["items"])
    assert client.get("/api/v1/keywords/explorer", params={"q": "kahve makinesi", "match": "exact"}).json()["total"] == 1
    assert client.get("/api/v1/keywords/explorer", params={"question": True}).json()["total"] == 1
    assert client.get("/api/v1/keywords/explorer", params={"exclude": ["fiyat"]}).json()["total"] == 2
    assert client.get("/api/v1/keywords/explorer", params={"q": "kahve", "min_words": 3}).json()["total"] == 1
    # SERP snapshots → KD + intent persisted
    s = client.post("/api/v1/keywords/serps", json={"keywords": ["kahve makinesi", "kahve makinesi fiyatları"], "confirm": True})
    assert s.json()["snapshots"] == 2
    row = next(i for i in client.get("/api/v1/keywords/explorer", params={"q": "fiyatları"}).json()["items"])
    assert row["kd"] is not None and row["kd_model"] == "kd-1" and "transactional" in row["intents"]
    assert client.get("/api/v1/keywords/explorer", params={"intent": "transactional"}).json()["total"] >= 1
    ov = client.get("/api/v1/keywords/overview", params={"keyword": "kahve makinesi"}).json()
    assert ov["metrics"]["volume"] is not None and ov["serp"]["available"] and len(ov["serp"]["organic"]) == 5
    assert ov["kd"]["missing_inputs"] == ["authority"] and ov["trend"]
    assert ov["first_party"]["gsc_volume_lower_bound"]["value"] is None  # no own GSC data → unknown, not 0


def test_local_org_without_provider_gets_not_configured(client, db, ch):
    r = client.post("/api/v1/keywords/lookup", json={"keywords": ["x"], "confirm": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "provider_not_configured"


def test_strategy_maps_clusters_to_own_urls(client, db, ch, demo):
    site = client.post("/api/v1/sites", json={"name": "Ex", "base_url": "https://example.com.tr"}).json()
    from app.normalize.locale import SearchContext
    from app.providers.base import Lineage, SerpItem, SerpResult
    from app.services.keywords import store
    kws = ["kahve makinesi", "kahve makineleri", "türk kahvesi makinesi", "çay demliği"]
    shared = [f"https://site{i}.com/k" for i in range(4)]
    for kw, urls in [(kws[0], shared), (kws[1], shared[:3] + ["https://x.com/1"]), (kws[2], shared[1:] + ["https://y.com/2"]),
                     (kws[3], ["https://c1.com", "https://c2.com", "https://c3.com"])]:
        store.ingest_serp(ch, SerpResult(kw, SearchContext(DB), [SerpItem(i + 1, i + 1, "organic", u, u.split("/")[2]) for i, u in enumerate(urls)],
                                         [], [], [], Lineage("DEMO", "mock", is_synthetic=True)))
    d = date.today() - timedelta(days=5)
    for q, page, impr in [("kahve makinesi", "https://example.com.tr/a", 500), ("kahve makineleri", "https://example.com.tr/b", 400)]:
        db.add(SearchPerformanceDaily(site_id=site["id"], date=d, page=page, query=q, device="MOBILE", country="tur",
                                      clicks=5, impressions=impr, ctr=0.01, position=8))
    db.commit()
    st = client.post("/api/v1/keywords/clusters", json={"keywords": kws, "site_id": site["id"], "min_shared": 3}).json()
    assert st["missing_serp"] == [] and st["lexical_clusters"]
    by_size = sorted(st["clusters"], key=lambda c: -len(c["keywords"]))
    big, tea = by_size[0], by_size[1]
    assert sorted(big["keywords"]) == sorted(kws[:3]) and len(big["shared_top10_urls"]) >= 2
    assert big["action"] == "resolve_cannibalization"
    assert set(big["cannibalization"]) == {"https://example.com.tr/a", "https://example.com.tr/b"}
    assert big["evidence"]["own_gsc_impressions_90d"] == 900
    assert tea["keywords"] == ["çay demliği"] and tea["action"] == "new_page_needed" and tea["mapped_url"] is None
    fp = client.get("/api/v1/keywords/first-party", params={"seed": "kahve"}).json()
    assert fp["source_class"] == "FIRST_PARTY" and fp["total"] == 2
    # first-party data never written to the global keyword dataset
    assert ch.query("SELECT count() AS n FROM keyword_metrics WHERE source_class = 'FIRST_PARTY'")[0]["n"] in (0, "0")


def test_keyword_lists_dedupe(client, db):
    lst = client.post("/api/v1/keyword-lists", json={"name": "Kahve"}).json()
    r = client.post(f"/api/v1/keyword-lists/{lst['id']}/items", json={"keywords": ["Kahve Makinesi", "kahve makinesi", "kahve makınesi", "çay"]}).json()
    assert r == {"added": 3, "skipped_duplicates": 1} or r["added"] == 2
    assert client.get(f"/api/v1/keyword-lists/{lst['id']}").json()["keyword_count"] == r["added"]


def test_module2_magic_compare_history_export_and_send_to_rank(client, db, ch, demo):
    kws = [
        "kahve makinesi",
        "kahve makinesi fiyatları",
        "en iyi kahve makinesi hangisi",
        "filtre kahve makinesi",
    ]
    r = client.post("/api/v1/keywords/lookup", json={"keywords": kws, "confirm": True, "request_id": "m2-1"})
    assert r.status_code == 200 and r.json()["ingested"] == 4
    client.post("/api/v1/keywords/serps", json={"keywords": kws[:2], "confirm": True, "request_id": "m2-2"})

    # 1. Keyword Magic Tool
    mg = client.get("/api/v1/keywords/magic", params={"seed": "kahve makinesi"})
    assert mg.status_code == 200
    mg_data = mg.json()
    assert mg_data["total"] >= 3
    assert len(mg_data["long_tail"]) >= 1
    assert len(mg_data["questions"]) >= 1
    assert len(mg_data["topic_groups"]) >= 1
    assert "provenance" in mg_data and mg_data["provenance"]["data_source"]

    # 2. Keyword Compare
    cmp_res = client.post("/api/v1/keywords/compare", json={"keywords": ["kahve makinesi", "kahve makinesi fiyatları"]})
    assert cmp_res.status_code == 200
    cmp_data = cmp_res.json()
    assert len(cmp_data["items"]) == 2
    assert all(i["volume"] is not None for i in cmp_data["items"])
    assert isinstance(cmp_data["shared_serp_domains"], list)

    # 3. Keyword History & Export
    hist = client.get("/api/v1/keywords/history")
    assert hist.status_code == 200
    hist_data = hist.json()
    assert len(hist_data["recent_searches"]) >= 1
    assert len(hist_data["recent_keywords"]) >= 3

    exp_csv = client.get("/api/v1/keywords/export", params={"q": "kahve", "format": "csv"})
    assert exp_csv.status_code == 200 and "keyword,volume,kd,cpc" in exp_csv.text
    exp_json = client.get("/api/v1/keywords/export", params={"q": "kahve", "format": "json"})
    assert exp_json.status_code == 200 and exp_json.json()["total"] >= 3

    # 4. Keyword List Export & Send to Rank Tracking
    lst = client.post("/api/v1/keyword-lists", json={"name": "Kahve Hedefleri"}).json()
    client.post(f"/api/v1/keyword-lists/{lst['id']}/items", json={"keywords": kws[:2], "tags": ["priority"]})
    lst_csv = client.get(f"/api/v1/keyword-lists/{lst['id']}/export", params={"format": "csv"})
    assert lst_csv.status_code == 200 and "kahve makinesi" in lst_csv.text

    rp = client.post("/api/v1/rank-projects", json={
        "name": "Kahve Rank",
        "own_domain": "example.com.tr",
        "database": DB,
        "devices": ["desktop"],
    }).json()
    sent = client.post(f"/api/v1/keyword-lists/{lst['id']}/send-to-rank", json={"rank_project_id": rp["id"]})
    assert sent.status_code == 200 and sent.json()["added"] == 2
    sent_again = client.post(f"/api/v1/keyword-lists/{lst['id']}/send-to-rank", json={"rank_project_id": rp["id"]})
    assert sent_again.json()["added"] == 0 and sent_again.json()["skipped_duplicates"] == 2
