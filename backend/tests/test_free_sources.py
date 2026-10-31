import asyncio
import json

import pytest
import respx
from sqlalchemy import func, select

from app.models import BingPerformanceDaily, IntegrationCredential
from app.normalize.locale import SearchContext
from app.providers import registry
from app.providers.free_sources import BING, GoogleAdsKeywordPlanner, parse_ms_date

TR = SearchContext("tr-TR:google.com.tr")


def test_ms_date():
    assert parse_ms_date("/Date(1696118400000-0700)/") == "2023-10-01" and parse_ms_date(None) is None


@respx.mock
def test_bing_connect_sync_and_separate_display(client, db, settings):
    settings(clickhouse_url="")
    site = client.post("/api/v1/sites", json={"name": "Ex", "base_url": "https://example.com.tr"}).json()
    assert client.post(f"/api/v1/sites/{site['id']}/sync/bing").json()["error"]["code"] == "integration_not_configured"
    assert client.post("/api/v1/integrations/bing/connect", json={"api_key": "BINGKEY-1234567890"}).json()["connected"]
    raw = db.scalar(select(IntegrationCredential.encrypted_refresh_token).where(IntegrationCredential.provider == "bing"))
    assert "BINGKEY" not in raw
    respx.get(f"{BING}/GetQueryStats").respond(json={"d": [
        {"Query": "kahve makinesi", "Clicks": 12, "Impressions": 300, "AvgImpressionPosition": 4.2, "AvgClickPosition": 3.1,
         "Date": "/Date(1761696000000)/"}]})
    respx.get(f"{BING}/GetLinkCounts").respond(json={"d": {"Links": [{"Url": "https://example.com.tr/", "Count": 1}]}})
    respx.get(f"{BING}/GetUrlLinks").respond(json={"d": {"Details": [{"Url": "https://blog.org/x", "AnchorText": "örnek"}]}})
    r = client.post(f"/api/v1/sites/{site['id']}/sync/bing").json()
    assert r["query_rows"] == 1 and r["source_class"] == "FIRST_PARTY"
    client.post(f"/api/v1/sites/{site['id']}/sync/bing")  # idempotent upsert
    assert db.scalar(select(func.count()).select_from(BingPerformanceDaily)) == 1
    perf = client.get(f"/api/v1/sites/{site['id']}/bing-performance").json()
    assert perf["engine"] == "bing" and perf["items"][0] == {"query": "kahve makinesi", "clicks": 12, "impressions": 300}
    gsc = client.get(f"/api/v1/sites/{site['id']}/search-performance").json()
    assert gsc["has_data"] is False  # Bing rows never leak into Google metrics


@respx.mock
def test_google_ads_keyword_planner(settings):
    registry.clear_overrides()
    p = GoogleAdsKeywordPlanner()
    assert not p.is_configured()
    settings(google_ads_developer_token="dev", google_ads_customer_id="1234567890", google_ads_refresh_token="1//r",
             google_client_id="cid", google_client_secret="sec", dataforseo_login="", dataforseo_password="")
    assert p.is_configured() and isinstance(registry.get("keyword_metrics"), GoogleAdsKeywordPlanner)
    respx.post("https://oauth2.googleapis.com/token").respond(json={"access_token": "ya29.x", "expires_in": 3600, "scope": ""})
    route = respx.post("https://googleads.googleapis.com/v21/customers/1234567890:generateKeywordHistoricalMetrics").respond(json={
        "results": [{"text": "kahve makinesi", "keywordMetrics": {"avgMonthlySearches": "14800", "competitionIndex": "87",
                     "lowTopOfPageBidMicros": "500000", "monthlySearchVolumes": [{"year": "2026", "month": "SEPTEMBER", "monthlySearches": "14800"},
                                                                                {"year": "2026", "month": "AUGUST", "monthlySearches": "12100"}]}},
                    {"text": "nadir", "keywordMetrics": {}}]})
    res = asyncio.run(p.keyword_metrics(TR, ["kahve makinesi", "nadir"]))
    body = json.loads(route.calls[0].request.content)
    assert body["geoTargetConstants"] == ["geoTargetConstants/2792"] and body["language"] == "languageConstants/1037"
    assert route.calls[0].request.headers["developer-token"] == "dev"
    assert res[0].search_volume == 14800 and res[0].volume_precision == "rounded" and res[0].monthly == [("2026-08", 12100), ("2026-09", 14800)]
    assert res[0].cpc is None and res[0].ads_competition == 0.87  # bids are not reported as CPC
    assert res[1].search_volume is None and res[1].volume_precision == "unknown"
    settings(dataforseo_login="l", dataforseo_password="p")
    assert registry.get("keyword_metrics").name == "dataforseo"  # paid provider has priority when configured
