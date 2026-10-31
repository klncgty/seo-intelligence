import asyncio
import json
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import respx
from sqlalchemy import func, select, text

from app.models import IntegrationCredential, OrganicLandingDaily, SearchPerformanceDaily, SearchTotalsDaily, Site, SyncJob
from app.services import google_service as gs
from app.worker import run_once

REFRESH = "1//REFRESH-TOKEN-SECRET-abcdefghijklmnop"


def configure(settings):
    settings(google_client_id="cid.apps.googleusercontent.com", google_client_secret="SECRET")


def add_site(db, **kw):
    s = Site(org_id=1, project_id=1, name="S", base_url="https://example.com/", allowed_hosts=["example.com"],
             conversion_events={"signup": ["sign_up"], "purchase": ["purchase"]}, crawl_settings={}, **kw)
    db.add(s)
    db.commit()
    return s


def test_disconnected_state_is_explicit(client, db):
    st = client.get("/api/v1/integrations/google/status").json()
    assert st["oauth_configured"] is False and st["connected"] is False and "GOOGLE_CLIENT_ID" in st["setup_instructions"]
    r = client.post("/api/v1/integrations/google/connect")
    assert r.status_code == 400 and r.json()["error"]["code"] == "integration_not_configured"
    site = add_site(db, gsc_property="sc-domain:example.com")
    r = client.post(f"/api/v1/sites/{site.id}/sync/search-console")
    assert r.status_code == 400 and r.json()["error"]["code"] == "integration_not_configured"
    perf = client.get(f"/api/v1/sites/{site.id}/search-performance").json()
    assert perf["has_data"] is False and perf["totals"] is None  # no fake zeros
    g = client.get(f"/api/v1/sites/{site.id}/growth").json()
    assert g["current"]["clicks"] is None and g["change_pct"]["clicks"] is None
    assert client.get(f"/api/v1/sites/{site.id}/integrations/search-console").json()["last_status"] == "never"


@respx.mock
def test_oauth_flow_state_csrf_and_encrypted_storage(client, db, settings):
    configure(settings)
    url = client.post("/api/v1/integrations/google/connect").json()["authorization_url"]
    q = parse_qs(urlsplit(url).query)
    assert q["access_type"] == ["offline"] and q["code_challenge_method"] == ["S256"]
    assert "webmasters.readonly" in q["scope"][0] and "analytics.readonly" in q["scope"][0]
    state = q["state"][0]
    respx.post("https://oauth2.googleapis.com/token").respond(json={
        "access_token": "ya29.ACCESS", "refresh_token": REFRESH, "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/webmasters.readonly https://www.googleapis.com/auth/analytics.readonly"})
    # forged state rejected, nothing stored
    r = client.get("/api/v1/integrations/google/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 303 and "google=error" in r.headers["location"]
    assert db.scalar(select(func.count()).select_from(IntegrationCredential)) == 0
    r = client.get("/api/v1/integrations/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert "google=connected" in r.headers["location"]
    # state is single-use
    r = client.get("/api/v1/integrations/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert "google=error" in r.headers["location"]
    raw = db.execute(text("SELECT encrypted_refresh_token FROM integration_credentials")).scalar_one()
    assert REFRESH not in raw and len(raw) > 50
    body = json.dumps(client.get("/api/v1/integrations/google/status").json())
    assert REFRESH not in body and "ya29" not in body and json.loads(body)["connected"] is True


def connect(db):
    from app.core import crypto
    db.add(IntegrationCredential(org_id=1, provider="google", encrypted_refresh_token=crypto.encrypt(REFRESH), scopes=[]))
    db.commit()


def gsc_mock(rows_totals, rows_detail):
    respx.post("https://oauth2.googleapis.com/token").respond(json={"access_token": "ya29.A", "expires_in": 3600, "scope": ""})

    def handler(request):
        body = json.loads(request.content)
        rows = rows_totals if body["dimensions"] == ["date", "device"] else rows_detail
        start = body.get("startRow", 0)
        return httpx.Response(200, json={"rows": rows[start:start + body["rowLimit"]]})
    respx.post(url__regex=r"https://www\.googleapis\.com/webmasters/v3/sites/.+/searchAnalytics/query").mock(side_effect=handler)


@respx.mock
def test_gsc_sync_idempotent_and_separate_totals(client, db, settings):
    configure(settings)
    connect(db)
    site = add_site(db, gsc_property="sc-domain:example.com")
    d = (date.today() - timedelta(days=5)).isoformat()
    totals = [{"keys": [d, "MOBILE"], "clicks": 100, "impressions": 1000, "ctr": 0.1, "position": 5.0}]
    detail = [{"keys": [d, "https://example.com/a", "shoes", "MOBILE", "usa"], "clicks": 60, "impressions": 500, "ctr": 0.12, "position": 4.0}]
    gsc_mock(totals, detail)
    for _ in range(2):  # run twice → no duplicates
        job = client.post(f"/api/v1/sites/{site.id}/sync/search-console")
        assert job.status_code == 202
        assert asyncio.run(run_once("w")) is True
    assert db.scalar(select(func.count()).select_from(SearchTotalsDaily)) == 1
    assert db.scalar(select(func.count()).select_from(SearchPerformanceDaily)) == 1
    st = client.get(f"/api/v1/sites/{site.id}/integrations/search-console").json()
    assert st["last_status"] == "success" and st["last_success_at"] and st["rows_synced"] == 2
    perf = client.get(f"/api/v1/sites/{site.id}/search-performance", params={"start": d, "end": d}).json()
    assert perf["totals"]["clicks"] == 100  # site totals, not the 60 from query-level rows
    pq = client.get(f"/api/v1/sites/{site.id}/search-performance", params={"start": d, "end": d, "dimension": "query"}).json()
    assert pq["rows"][0] == {"key": "shoes", "clicks": 60, "impressions": 500, "ctr": 0.12, "position": 4.0} and pq["note"]


@respx.mock
def test_sync_error_is_reported(client, db, settings):
    configure(settings)
    connect(db)
    site = add_site(db, gsc_property="https://example.com/")
    respx.post("https://oauth2.googleapis.com/token").respond(400, json={"error": "invalid_grant"})
    client.post(f"/api/v1/sites/{site.id}/sync/search-console")
    asyncio.run(run_once("w"))
    st = client.get(f"/api/v1/sites/{site.id}/integrations/search-console").json()
    assert st["last_status"] == "error" and st["last_error"] and REFRESH not in st["last_error"]
    assert db.scalar(select(SyncJob.status)) == "error"


@respx.mock
def test_ga4_sync_idempotent_and_conversions(client, db, settings):
    configure(settings)
    connect(db)
    site = add_site(db, ga4_property_id="properties/123")
    respx.post("https://oauth2.googleapis.com/token").respond(json={"access_token": "ya29.A", "expires_in": 3600, "scope": ""})
    d = (date.today() - timedelta(days=4)).strftime("%Y%m%d")

    def report(request):
        body = json.loads(request.content)
        dims = [x["name"] for x in body["dimensions"]]
        if "eventName" in dims:
            rows = [{"dimensionValues": [{"value": v} for v in (d, "/a", "Organic Search", "google / organic", "sign_up")],
                     "metricValues": [{"value": "4"}]},
                    {"dimensionValues": [{"value": v} for v in (d, "/a", "Organic Search", "google / organic", "purchase")],
                     "metricValues": [{"value": "1"}]}]
            mh = [{"name": "eventCount", "type": "TYPE_INTEGER"}]
        else:
            rows = [{"dimensionValues": [{"value": v} for v in (d, "/a", "Organic Search", "google / organic")],
                     "metricValues": [{"value": v} for v in ("200", "150", "140", "9", "99.5")]},
                    {"dimensionValues": [{"value": v} for v in (d, "/a", "Paid Search", "google / cpc")],
                     "metricValues": [{"value": v} for v in ("50", "40", "40", "1", "0")]}]
            mh = [{"name": m["name"], "type": "TYPE_FLOAT"} for m in body["metrics"]]
        assert body["returnPropertyQuota"] is True
        return httpx.Response(200, json={"dimensionHeaders": [{"name": n} for n in dims], "metricHeaders": mh,
                                         "rows": rows, "rowCount": len(rows),
                                         "metadata": {"subjectToThresholding": True, "dataLossFromOtherRow": False},
                                         "propertyQuota": {"tokensPerDay": {"consumed": 12, "remaining": 199988}}})
    respx.post("https://analyticsdata.googleapis.com/v1beta/properties/123:runReport").mock(side_effect=report)
    for _ in range(2):
        assert client.post(f"/api/v1/sites/{site.id}/sync/analytics").status_code == 202
        asyncio.run(run_once("w"))
    assert db.scalar(select(func.count()).select_from(OrganicLandingDaily)) == 2
    iso = (date.today() - timedelta(days=4)).isoformat()
    conv = client.get(f"/api/v1/sites/{site.id}/conversions", params={"start": iso, "end": iso}).json()
    assert conv["totals"]["sessions"] == 200  # organic only; paid excluded
    assert conv["totals"]["signups"] == 4 and conv["totals"]["purchases"] == 1 and conv["totals"]["revenue"] == 99.5
    st = client.get(f"/api/v1/sites/{site.id}/integrations/analytics").json()["last_sync_metadata"]
    assert st["subject_to_thresholding"] is True and st["property_quota"]["tokensPerDay"]["remaining"] == 199988
    dq = client.get(f"/api/v1/sites/{site.id}/data-quality").json()["ga4_data_quality"]
    assert dq["subject_to_thresholding"] is True and dq["data_loss_from_other_row"] is False
    lp = client.get(f"/api/v1/sites/{site.id}/landing-pages", params={"start": iso, "end": iso}).json()
    assert lp["items"][0]["conversion_rate"] == 5 / 200


def test_unconfigured_events_stay_null(client, db):
    site = add_site(db)
    d = date.today() - timedelta(days=4)
    db.add(OrganicLandingDaily(site_id=site.id, date=d, landing_page="/x", channel_group="Organic Search",
                               source_medium="google / organic", sessions=10, total_users=8))
    db.commit()
    conv = client.get(f"/api/v1/sites/{site.id}/conversions", params={"start": d.isoformat(), "end": d.isoformat()}).json()
    assert conv["totals"]["sessions"] == 10 and conv["totals"]["signups"] is None and conv["totals"]["revenue"] is None
    assert gs.integration_status(db, site, "analytics")["configured"] is False
