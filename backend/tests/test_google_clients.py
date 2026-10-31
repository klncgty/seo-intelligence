import json
from datetime import date

import httpx
import pytest
import respx

from app.services.integrations import analytics as ga
from app.services.integrations import retry as rt
from app.services.integrations import search_console as sc

D1, D2 = date(2026, 1, 1), date(2026, 1, 7)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(rt, "DEFAULT_BASE_DELAY", 0)


@respx.mock
async def test_list_sites():
    respx.get("https://www.googleapis.com/webmasters/v3/sites").mock(
        return_value=httpx.Response(200, json={"siteEntry": [{"siteUrl": "sc-domain:a.com", "permissionLevel": "siteOwner"}]})
    )
    async with httpx.AsyncClient() as c:
        assert await sc.list_sites(c, "t") == [{"site_url": "sc-domain:a.com", "permission_level": "siteOwner"}]


def _row(i):
    return {"keys": ["2026-01-01", f"https://a.com/{i}", "q", "MOBILE", "usa"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 2.5}


@respx.mock
async def test_gsc_pagination_and_encoding():
    route = respx.post(url__regex=r".*/sites/sc-domain%3Aa\.com/searchAnalytics/query").mock(
        side_effect=[httpx.Response(200, json={"rows": [_row(1), _row(2)]}), httpx.Response(200, json={"rows": [_row(3)]})]
    )
    async with httpx.AsyncClient() as c:
        rows = [r async for r in sc.iter_search_analytics(c, "t", "sc-domain:a.com", D1, D2, row_limit=2)]
    assert len(rows) == 3 and route.call_count == 2
    assert json.loads(route.calls[1].request.content)["startRow"] == 2
    assert rows[0] == {"date": "2026-01-01", "page": "https://a.com/1", "query": "q", "device": "MOBILE", "country": "usa",
                       "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 2.5}
    assert route.calls[0].request.headers["authorization"] == "Bearer t"


@respx.mock
async def test_gsc_max_rows():
    respx.post(url__regex=r".*searchAnalytics/query").mock(
        return_value=httpx.Response(200, json={"rows": [_row(1), _row(2)]})
    )
    async with httpx.AsyncClient() as c:
        rows = [r async for r in sc.iter_search_analytics(c, "t", "https://a.com/", D1, D2, row_limit=2, max_rows=3)]
    assert len(rows) == 3


@respx.mock
async def test_list_properties_pagination():
    route = respx.get(f"{ga.ADMIN_BASE}/accountSummaries").mock(
        side_effect=[
            httpx.Response(200, json={"accountSummaries": [{"account": "accounts/1", "propertySummaries": [{"property": "properties/10", "displayName": "A"}]}], "nextPageToken": "n"}),
            httpx.Response(200, json={"accountSummaries": [{"account": "accounts/2", "propertySummaries": [{"property": "properties/20", "displayName": "B"}]}]}),
        ]
    )
    async with httpx.AsyncClient() as c:
        props = await ga.list_properties(c, "t")
    assert props == [
        {"property_id": "properties/10", "display_name": "A", "account": "accounts/1"},
        {"property_id": "properties/20", "display_name": "B", "account": "accounts/2"},
    ]
    assert route.calls[1].request.url.params["pageToken"] == "n"


def report(dims, mets, rows):
    return {
        "dimensionHeaders": [{"name": d} for d in dims],
        "metricHeaders": [{"name": m} for m in mets],
        "rows": [{"dimensionValues": [{"value": v} for v in d], "metricValues": [{"value": v} for v in m]} for d, m in rows],
        "rowCount": len(rows),
    }


@respx.mock
async def test_run_report_parsing():
    respx.post(f"{ga.DATA_BASE}/properties/1:runReport").mock(
        return_value=httpx.Response(200, json=report(["country"], ["sessions", "bounceRate"], [(["TR"], ["5", "0.25"])]))
    )
    async with httpx.AsyncClient() as c:
        rows = await ga.run_report(c, "t", "properties/1", D1, D2, ["country"], ["sessions", "bounceRate"])
    assert rows == [{"country": "TR", "sessions": 5, "bounceRate": 0.25}]


DIMS = ["date", "landingPagePlusQueryString", "sessionDefaultChannelGroup", "sessionSourceMedium"]


@respx.mock
async def test_fetch_landing_daily_merge_and_none():
    seen: list = []

    def handler(request):
        body = json.loads(request.content)
        if "eventName" in [d["name"] for d in body["dimensions"]]:
            seen.append(body["dimensionFilter"]["filter"]["inListFilter"]["values"])
            return httpx.Response(200, json=report(DIMS + ["eventName"], ["eventCount"], [
                (["20260101", "/a", "Organic Search", "google / organic", "sign_up"], ["3"])]))
        return httpx.Response(200, json=report(DIMS, ["sessions", "totalUsers", "activeUsers", "keyEvents", "purchaseRevenue"], [
            (["20260101", "/a", "Organic Search", "google / organic"], ["10", "8", "7", "2", "12.5"])]))

    respx.post(f"{ga.DATA_BASE}/properties/1:runReport").mock(side_effect=handler)
    async with httpx.AsyncClient() as c:
        rows = await ga.fetch_landing_daily(c, "t", "properties/1", D1, D2, {"signups": ["sign_up"], "purchases": []})
    assert seen == [["sign_up"]]
    assert rows == [{
        "date": "2026-01-01", "landing_page": "/a", "channel_group": "Organic Search", "source_medium": "google / organic",
        "sessions": 10, "total_users": 8, "active_users": 7, "key_events": 2, "purchase_revenue": 12.5,
        "signups": 3, "purchases": None,
    }]


@respx.mock
async def test_fetch_landing_daily_metric_fallback():
    calls = []

    def handler(request):
        names = [m["name"] for m in json.loads(request.content)["metrics"]]
        calls.append(names)
        if "keyEvents" in names:
            return httpx.Response(400, json={"error": {"message": "Field keyEvents is not a valid metric", "status": "INVALID_ARGUMENT"}})
        return httpx.Response(200, json=report(DIMS, names, [(["20260101", "/a", "Direct", "(direct) / (none)"], ["4", "3", "3"])]))

    respx.post(f"{ga.DATA_BASE}/properties/1:runReport").mock(side_effect=handler)
    async with httpx.AsyncClient() as c:
        rows = await ga.fetch_landing_daily(c, "t", "properties/1", D1, D2, {})
    assert len(calls) == 2
    r = rows[0]
    assert (r["sessions"], r["key_events"], r["purchase_revenue"], r["signups"], r["purchases"]) == (4, None, None, None, None)
