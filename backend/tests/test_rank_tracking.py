import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from app.infra.clickhouse import ClickHouse
from app.models import Job, RankAlert, RankProject, Schedule
from app.normalize.locale import SearchContext
from app.providers import registry, runtime
from app.providers.base import Lineage, SerpItem, SerpResult
from app.services import rank_tracking as rt
from app.services.keywords import store
from app.worker import run_once

DB = "tr-TR:google.com.tr"


def test_ctr_visibility_and_positions():
    assert rt.ctr(1) == 0.28 and rt.ctr(15) == 0.01 and rt.ctr(None) == 0 and rt.ctr(50) == 0
    assert rt.visibility([1, 1]) == 100.0 and rt.visibility([None, None]) == 0.0 and rt.visibility([]) is None
    assert rt.visibility([1, None]) == 50.0
    organic = [{"rank_group": 1, "domain": "www.a.com", "url": "https://www.a.com/x"},
               {"rank_group": 2, "domain": "b.com.tr", "url": "https://b.com.tr/1"},
               {"rank_group": 3, "domain": "a.com", "url": "https://a.com/y"}]
    pos = rt.compute_positions(organic, ["a.com", "b.com.tr", "c.com"])
    assert pos["a.com"] == {"position": 1, "landing_url": "https://www.a.com/x"} and pos["c.com"]["position"] is None


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


def test_volatility_sensor(ch):
    ctx = SearchContext(DB)
    d1, d2 = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    stable = [f"https://s{i}.com/" for i in range(10)]
    for kw in ("a", "b"):
        store.ingest_serp(ch, SerpResult(kw, ctx, [SerpItem(i + 1, i + 1, "organic", u, u[8:-1]) for i, u in enumerate(stable)],
                                         [], [], [], Lineage("DEMO", "mock")), d1)
    store.ingest_serp(ch, SerpResult("a", ctx, [SerpItem(i + 1, i + 1, "organic", u, u[8:-1]) for i, u in enumerate(stable)],
                                     [], [], [], Lineage("DEMO", "mock")), d2)
    shuffled = list(reversed(stable))
    store.ingest_serp(ch, SerpResult("b", ctx, [SerpItem(i + 1, i + 1, "organic", u, u[8:-1]) for i, u in enumerate(shuffled)],
                                     [], [], [], Lineage("DEMO", "mock")), d2)
    v = rt.volatility(ch, DB)
    assert len(v["series"]) == 1 and v["series"][0]["keywords"] == 2
    assert v["series"][0]["score"] == 2.5 and v["series"][0]["label"] == "normal"  # (0 + 5) / 2
    assert "not a claim" in v["note"]


def test_rank_project_flow_dag_and_gsc_side_by_side(client, db, ch):
    db.execute(text("UPDATE organizations SET is_demo = true WHERE id = 1"))
    db.commit()
    rp = client.post("/api/v1/rank-projects", json={"name": "TR", "own_domain": "https://www.example.com.tr/",
                                                    "competitor_domains": ["rakip1.com.tr", "rakip2.com", "example.com.tr"],
                                                    "devices": ["desktop", "mobile"]}).json()
    assert rp["own_domain"] == "example.com.tr" and rp["competitor_domains"] == ["rakip1.com.tr", "rakip2.com"]
    add = client.post(f"/api/v1/rank-projects/{rp['id']}/keywords", json={"keywords": ["kahve makinesi", "Kahve Makinesi", "çay"],
                                                                          "important": True}).json()
    assert add["added"] == 2 and add["monthly_estimate"]["serp_requests_per_month"] == 2 * 2 * 30
    assert client.post(f"/api/v1/rank-projects/{rp['id']}/check").status_code == 428  # not confirmed
    assert client.post(f"/api/v1/rank-projects/{rp['id']}/confirm-budget").json()["budget_confirmed"]
    assert db.scalar(select(Schedule.kind).where(Schedule.name == f"rank_project:{rp['id']}")) == "rank_dag"
    jobs = client.post(f"/api/v1/rank-projects/{rp['id']}/check").json()
    for _ in range(6):
        asyncio.run(run_once("w"))
    db.expire_all()
    assert db.get(Job, jobs["check_job_id"]).status == "succeeded", db.get(Job, jobs["check_job_id"]).error
    assert db.get(Job, jobs["alerts_job_id"]).status == "succeeded"  # DAG: ran after check
    ov = client.get(f"/api/v1/rank-projects/{rp['id']}/overview").json()
    assert ov["available"] and set(ov["domains"]) == {"example.com.tr", "rakip1.com.tr", "rakip2.com"}
    day = ov["series"][-1]
    assert day["visibility"]["example.com.tr"] is not None and ov["source_classes"]["visibility"] == "MODELED"
    assert ov["source_classes"]["positions"] == "DEMO"
    cmp = client.get(f"/api/v1/rank-projects/{rp['id']}/gsc-comparison").json()
    item = cmp["items"][0]
    assert item["tracked_rank"]["source_class"] == "PROVIDER" and item["gsc_avg_position"]["source_class"] == "FIRST_PARTY"
    assert item["gsc_avg_position"]["value"] is None and cmp["why_they_differ"]  # no GSC → unknown, not merged
    kid = client.get(f"/api/v1/rank-projects/{rp['id']}/keywords").json()["items"][0]["id"]
    assert client.get(f"/api/v1/rank-projects/{rp['id']}/keywords/{kid}/history").json()["history"]


def test_alert_rules(client, db, ch):
    rp = RankProject(org_id=1, project_id=1, name="x", own_domain="me.com", competitor_domains=["rival.com"], database=DB,
                     devices=["desktop"], locations=[""], budget_confirmed=True)
    db.add(rp)
    db.flush()
    from app.models import TrackedKeyword
    kw = TrackedKeyword(org_id=1, rank_project_id=rp.id, keyword="kahve", keyword_key="kahve", important=True)
    db.add(kw)
    db.commit()
    d1, d2 = (date.today() - timedelta(days=1)).isoformat(), date.today().isoformat()

    def row(d, dom, pos, url="", feats=None):
        return {"tenant_id": 1, "project_id": rp.id, "tracked_keyword_id": kw.id, "keyword_key": "kahve", "locale": DB,
                "device": "desktop", "location": "", "check_date": d, "domain": dom, "position": pos, "landing_url": url,
                "serp_features_owned": feats or [], "source_class": "DEMO", "provider": "mock",
                "fetched_at": "2026-10-29 00:00:00", "is_synthetic": 1, "version": 1}
    ch.insert("rank_tracking_results", [row(d1, "me.com", 2, "https://me.com/a", ["featured_snippet"]), row(d1, "rival.com", 8),
                                        row(d2, "me.com", 9, "https://me.com/b"), row(d2, "rival.com", 2)])
    created = rt.evaluate_alerts(db, ch, rp)
    assert {a.alert_type for a in created} == {"rank_drop", "landing_url_change", "serp_feature_lost", "competitor_top3"}
    assert rt.evaluate_alerts(db, ch, rp) == []  # deduped
    assert db.scalar(select(RankAlert).where(RankAlert.alert_type == "rank_drop")).details == {"from": 2, "to": 9, "device": "desktop", "location": ""}
    # tenant isolation in ClickHouse: another tenant sees nothing
    assert rt.history(ch, 999, rp.id) == []


def test_module3_rankings_matrix_and_extended_alerts(client, db, ch):
    from app.models import TrackedKeyword

    rp = RankProject(
        org_id=1,
        project_id=1,
        name="Mod3",
        own_domain="me.com",
        competitor_domains=["rival.com"],
        database=DB,
        devices=["desktop", "mobile"],
        locations=[""],
        budget_confirmed=True,
    )
    db.add(rp)
    db.flush()
    kw_rise = TrackedKeyword(org_id=1, rank_project_id=rp.id, keyword="espresso", keyword_key="espresso", important=True)
    kw_fall = TrackedKeyword(org_id=1, rank_project_id=rp.id, keyword="filtre kahve", keyword_key="filtre kahve", important=True)
    db.add_all([kw_rise, kw_fall])
    db.commit()

    d1 = (date.today() - timedelta(days=2)).isoformat()
    d2 = (date.today() - timedelta(days=1)).isoformat()
    d3 = date.today().isoformat()

    def row(d, kid, key, dom, pos, dev="desktop", url="https://me.com/p", feats=None):
        return {
            "tenant_id": 1,
            "project_id": rp.id,
            "tracked_keyword_id": kid,
            "keyword_key": key,
            "locale": DB,
            "device": dev,
            "location": "",
            "check_date": d,
            "domain": dom,
            "position": pos,
            "landing_url": url,
            "serp_features_owned": feats or [],
            "source_class": "DEMO",
            "provider": "mock",
            "fetched_at": "2026-10-30 00:00:00",
            "is_synthetic": 1,
            "version": 1,
        }

    # kw_rise: 14 -> 9 -> 2 (overtakes rival.com 6 -> 5, enters top 3)
    # kw_fall: 4 -> 6 -> 8 (sustained decline over 3 checks, falls behind rival.com 7 -> 5 on d3 with drop=2 < 5)
    ch.insert("rank_tracking_results", [
        row(d1, kw_rise.id, "espresso", "me.com", 14),
        row(d1, kw_rise.id, "espresso", "rival.com", 6),
        row(d2, kw_rise.id, "espresso", "me.com", 9),
        row(d2, kw_rise.id, "espresso", "rival.com", 6),
        row(d3, kw_rise.id, "espresso", "me.com", 2, feats=["featured_snippet"]),
        row(d3, kw_rise.id, "espresso", "rival.com", 5),
        row(d1, kw_fall.id, "filtre kahve", "me.com", 4),
        row(d1, kw_fall.id, "filtre kahve", "rival.com", 7),
        row(d2, kw_fall.id, "filtre kahve", "me.com", 6),
        row(d2, kw_fall.id, "filtre kahve", "rival.com", 7),
        row(d3, kw_fall.id, "filtre kahve", "me.com", 8),
        row(d3, kw_fall.id, "filtre kahve", "rival.com", 5),
    ])
    store.ingest_serp(
        ch,
        SerpResult(
            "espresso",
            SearchContext(DB, "mobile"),
            [SerpItem(4, 4, "organic", "https://me.com/p", "me.com")],
            [],
            [],
            [],
            Lineage("DEMO", "mock", is_synthetic=True),
        ),
        date.today(),
    )

    created = rt.evaluate_alerts(db, ch, rp)
    kinds = {a.alert_type for a in created}
    assert {"top3_entry", "competitor_overtake", "sustained_decline", "falling_behind_competitor"} <= kinds

    ov = client.get(f"/api/v1/rank-projects/{rp.id}/overview").json()
    assert ov["available"] is True
    dist = ov["series"][-1]["distribution"]["me.com"]
    assert dist["top3"] >= 1 and dist["top10"] >= 2 and dist["top20"] >= 2 and dist["avg_position"] is not None
    assert ov["ranking_changes"]["counts"]["improved"] >= 1
    assert ov["ranking_changes"]["counts"]["declined"] >= 1
    assert ov["serp_features_summary"]["featured_snippet_count"] >= 1

    rk = client.get(f"/api/v1/rank-projects/{rp.id}/rankings").json()
    assert rk["total"] == 2
    by_kw = {i["keyword"]: i for i in rk["items"]}
    esp = by_kw["espresso"]
    assert esp["current_position"] == 2
    assert esp["previous_position"] == 9
    assert esp["delta_1d"] == 7
    assert esp["best_position"] == 2 and esp["worst_position"] == 14
    assert esp["desktop_position"] == 2 and esp["mobile_position"] == 4
    assert esp["mobile_vs_desktop_diff"] == -2
    assert esp["featured_snippet_owned"] is True
    assert esp["competitor_positions"]["rival.com"] == 5
    assert esp["provenance"]["data_source"] == "mock"
