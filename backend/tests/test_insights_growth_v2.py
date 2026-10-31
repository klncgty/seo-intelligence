from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import CalendarEvent, GrowthOpportunity, OrganicLandingDaily, SearchPerformanceDaily, SearchTotalsDaily
from app.services import analytics_depth as ad
from app.services import growth_v2


def mk_site(client, **settings):
    s = client.post("/api/v1/sites", json={"name": "Ex", "base_url": "https://example.com.tr"}).json()
    if settings:
        assert client.patch(f"/api/v1/sites/{s['id']}", json=settings).status_code == 200
    return s


def test_wilson_and_canonical_path():
    w = ad.wilson(5, 100)
    assert w["low"] < 0.05 < w["high"] and ad.wilson(0, 0) is None
    assert ad.canonical_path("https://e.com/Kahve/?utm=1#x") == "/kahve" and ad.canonical_path("/") == "/"


def test_anomaly_with_dow_baseline_holiday_and_preliminary(client, db):
    s = mk_site(client)
    end = date.today() - timedelta(days=1)
    spike_day = end - timedelta(days=20)
    for i in range(150):
        d = end - timedelta(days=i)
        v = 200 + (60 if d.weekday() >= 5 else 0)  # weekly seasonality must not trigger
        if d == spike_day:
            v = 30
        if d >= date.today() - timedelta(days=3):
            v = 5  # preliminary days look like a crash but must be ignored
        db.add(SearchTotalsDaily(site_id=s["id"], date=d, device="MOBILE", clicks=v, impressions=v * 10, ctr=0.1, position=5))
    db.add(CalendarEvent(org_id=1, kind="campaign", name="Kampanya", start_date=spike_day, end_date=spike_day, source="user"))
    db.commit()
    an = client.get(f"/api/v1/sites/{s['id']}/anomalies").json()
    assert [a["date"] for a in an["anomalies"]] == [spike_day.isoformat()]
    assert an["anomalies"][0]["direction"] == "drop" and an["anomalies"][0]["calendar_events"] == ["Kampanya"]
    fc = client.get(f"/api/v1/sites/{s['id']}/forecast").json()
    assert fc["available"] and fc["source_class"] == "MODELED" and all(p["low"] <= p["yhat"] <= p["high"] for p in fc["points"])
    dq = client.get(f"/api/v1/sites/{s['id']}/data-quality").json()
    assert dq["gsc_timezone"] == "America/Los_Angeles" and dq["timezone_warning"]


def test_forecast_requires_history(client, db):
    s = mk_site(client)
    assert client.get(f"/api/v1/sites/{s['id']}/forecast").json()["available"] is False


def test_coverage_join_segments_brand_ai(client, db):
    s = mk_site(client, page_groups=[{"name": "ürün", "pattern": "^/urun/"}, {"name": "blog", "pattern": "^/blog/"}],
                brand_terms=["Örnek"], money_page_patterns=["^/urun/"])
    assert client.patch(f"/api/v1/sites/{s['id']}", json={"page_groups": [{"name": "x", "pattern": "("}]}).status_code == 422
    d = date.today() - timedelta(days=6)
    db.add(SearchTotalsDaily(site_id=s["id"], date=d, device="MOBILE", clicks=100, impressions=2000, ctr=0.05, position=6))
    for page, q, c, i in [("https://example.com.tr/urun/1", "kahve makinesi", 40, 800), ("https://example.com.tr/blog/a/", "örnek mağaza", 20, 300),
                          ("https://example.com.tr/hakkinda", "ornek magaza iletisim", 10, 100)]:
        db.add(SearchPerformanceDaily(site_id=s["id"], date=d, page=page, query=q, device="MOBILE", country="tur", clicks=c,
                                      impressions=i, ctr=c / i, position=5))
    for lp, sm, ch, sess, su in [("/urun/1", "google / organic", "Organic Search", 50, 3), ("/blog/a", "google / organic", "Organic Search", 25, 0),
                                 ("/sadece-ga", "google / organic", "Organic Search", 5, 0), ("/urun/1", "chatgpt.com / referral", "Referral", 7, 1)]:
        db.add(OrganicLandingDaily(site_id=s["id"], date=d, landing_page=lp, channel_group=ch, source_medium=sm, sessions=sess,
                                   total_users=sess, signups=su, purchases=0))
    db.commit()
    dq = client.get(f"/api/v1/sites/{s['id']}/data-quality").json()
    assert dq["query_coverage_pct"] == 70.0
    jc = client.get(f"/api/v1/sites/{s['id']}/join-coverage").json()
    assert jc["gsc_clicks_matched_pct"] == round(60 / 70 * 100, 1) and "/sadece-ga" in jc["unmatched_ga4_landing_pages"]
    seg = client.get(f"/api/v1/sites/{s['id']}/segments").json()
    assert seg["page_groups"]["ürün"]["clicks"] == 40 and seg["page_groups"]["ürün"]["sessions"] == 50
    assert seg["page_groups"]["ürün"]["conversion_rate"]["rate"] == 0.06
    assert seg["brand_split"] == {"brand_clicks": 30, "non_brand_clicks": 40, "note": seg["brand_split"]["note"]}
    assert seg["money_pages"]["clicks"] == 40
    assert seg["ai_referrals"]["items"][0]["source_medium"] == "chatgpt.com / referral"


def _opp(db, site_id, key, status="new", sources=("gsc",), url="https://example.com.tr/p", implemented_at=None, **kw):
    o = GrowthOpportunity(site_id=site_id, key=key, url=url, opportunity_type=kw.get("type", "low_ctr"), title="t", description="d",
                          evidence=kw.get("evidence", {}), data_sources=list(sources), confidence=kw.get("confidence", 0.9),
                          effort=kw.get("effort", "low"), priority_score=kw.get("priority", 50), recommended_action="a",
                          validation_metric="v", limitations="l", status=status, implemented_at=implemented_at,
                          date_range=kw.get("date_range"), impact_estimate=kw.get("impact"))
    db.add(o)
    db.commit()
    return o


def test_lifecycle_confidence_cap_and_reopen(client, db):
    s = mk_site(client)
    stale = _opp(db, s["id"], "stale|x")
    capped = _opp(db, s["id"], "gap|y", sources=["observed:serp", "provider:keyword_metrics"], confidence=0.9)
    from tests.test_api_sites_growth import seed
    seed(db, s["id"])  # synthetic GSC/GA4 evidence (low CTR etc.)
    client.post(f"/api/v1/sites/{s['id']}/opportunities/regenerate")
    impl = db.scalar(select(GrowthOpportunity).where(GrowthOpportunity.key.like("low_ctr|%")))
    impl.status, impl.implemented_at = "implemented", datetime.now(UTC) - timedelta(days=40)
    db.commit()
    res = client.post(f"/api/v1/sites/{s['id']}/opportunities/regenerate").json()  # evidence still present → regression
    db.expire_all()
    assert db.get(GrowthOpportunity, stale.id).resolution_suggested_at is not None
    assert db.get(GrowthOpportunity, capped.id).confidence == 0.6  # weakest source OBSERVED caps confidence
    reopened = db.get(GrowthOpportunity, impl.id)
    assert reopened.status == "new" and reopened.reopened_count == 1 and res["lifecycle"]["reopened"] == 1
    assert reopened.evidence["reopened"]["reason"].startswith("evidence reappeared")
    ie = reopened.impact_estimate
    assert ie["p10"] <= ie["p50"] <= ie["p90"] and "bootstrap" in ie["method"]


def _series(db, site_id, page, start, days, page_fn, total_fn):
    for i in range(days):
        d = start + timedelta(days=i)
        pc = page_fn(d)
        db.add(SearchPerformanceDaily(site_id=site_id, date=d, page=page, query="q", device="MOBILE", country="tur", clicks=pc,
                                      impressions=pc * 20, ctr=0.05, position=5))
        db.add(SearchTotalsDaily(site_id=site_id, date=d, device="MOBILE", clicks=total_fn(d) + pc, impressions=10000, ctr=0.05, position=5))
    db.commit()


@pytest.mark.parametrize("effect,expected", [(25, "positive"), (0, "inconclusive")])
def test_did_measurement_with_injected_effect(client, db, effect, expected):
    s = mk_site(client)
    t0 = date.today() - timedelta(days=40)
    page = "https://example.com.tr/target"
    import random
    rnd = random.Random(3)
    _series(db, s["id"], page, t0 - timedelta(days=28), 57,
            lambda d: 50 + rnd.randint(-5, 5) + (effect if d > t0 else 0), lambda d: 1000 + rnd.randint(-40, 40))
    o = _opp(db, s["id"], f"m|{effect}", status="implemented", url=page,
             implemented_at=datetime.combine(t0, datetime.min.time(), tzinfo=UTC), impact={"p50": 500, "value": 500})
    m = client.post(f"/api/v1/opportunities/{o.id}/measure").json()
    assert m["status"] == expected
    if effect:
        assert m["ci95"][0] > 0 and abs(m["daily_clicks_effect"] - effect) < 8
    cal = client.get("/api/v1/opportunities-calibration").json()
    assert cal["measured"] == 1


def test_measurement_pending_and_portfolio(client, db):
    s = mk_site(client)
    o = _opp(db, s["id"], "recent", status="implemented", implemented_at=datetime.now(UTC) - timedelta(days=5))
    assert client.post(f"/api/v1/opportunities/{o.id}/measure").json()["status"] == "pending"
    for i, eff in enumerate(["low", "medium", "high", "low"]):
        _opp(db, s["id"], f"p{i}", effort=eff, priority=90 - i)
    pf = client.get("/api/v1/portfolio", params={"weekly_hours": 12, "weeks": 4}).json()
    assert pf["plan"][0]["items"][0]["priority"] == 90 and all(w["hours"] <= 12 or len(w["items"]) == 1 for w in pf["plan"])
    assert pf["kanban"]["new"] == 4
