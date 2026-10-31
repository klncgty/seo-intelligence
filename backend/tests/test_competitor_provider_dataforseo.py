import httpx
import pytest
import respx

from app.services.competitors.providers import (
    BacklinkSummary,
    DataForSEOError,
    DataForSEOProvider,
    ProviderNotConfigured,
    UnavailableProvider,
    compare_backlinks,
    get_provider,
)

ROOT = "https://api.dataforseo.com/v3"


@pytest.mark.asyncio
async def test_unavailable_provider_never_returns_fake_data():
    provider = UnavailableProvider("Set credentials.")
    assert provider.is_configured() is False
    with pytest.raises(ProviderNotConfigured, match="Set credentials"):
        await provider.keyword_gap("own.example", ["rival.example"])
    with pytest.raises(ProviderNotConfigured, match="Set credentials"):
        await provider.backlink_summary("own.example")
    with pytest.raises(ProviderNotConfigured, match="Set credentials"):
        await provider.referring_domains("own.example")


def test_registry_falls_back_when_provider_or_credentials_are_missing():
    assert isinstance(get_provider(None, "", ""), UnavailableProvider)
    assert isinstance(get_provider("unsupported", "login", "password"), UnavailableProvider)
    assert isinstance(get_provider("dataforseo", " ", "password"), UnavailableProvider)
    provider = get_provider("data-for-seo", "login", "password")
    assert isinstance(provider, DataForSEOProvider)
    assert provider.is_configured()


@pytest.mark.asyncio
async def test_dataforseo_keyword_gap_request_and_defensive_parsing():
    payload = {
        "status_code": 20000,
        "tasks": [
            {
                "status_code": 20000,
                "data": {"target2": "rival.example"},
                "result": [{
                    "items": [
                        {
                            "keyword_data": {
                                "keyword": "seo tools",
                                "keyword_info": {"search_volume": 1200},
                            },
                            "first_domain_serp_element": {
                                "rank_group": "8",
                                "url": "https://own.example/seo-tools",
                            },
                            "second_domain_serp_element": {
                                "serp_item": {
                                    "rank_group": 3,
                                    "url": "https://rival.example/tools",
                                }
                            },
                        },
                        {"keyword_data": {"keyword": "bad row"}},
                        None,
                    ]
                }],
            },
            {"status_code": 20000, "data": {"target2": "other.example"}, "result": []},
        ],
    }
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(
                f"{ROOT}/dataforseo_labs/google/domain_intersection/live"
            ).mock(return_value=httpx.Response(200, json=payload))
            provider = DataForSEOProvider("api-login", "api-password", client)
            result = await provider.keyword_gap(
                "own.example", ["other.example", "rival.example"], limit=30
            )

    assert route.called
    request = route.calls[0].request
    assert request.headers["Authorization"].startswith("Basic ")
    assert len(request.read()) > 0
    sent = route.calls[0].request.content.decode()
    assert '"target1":"own.example"' in sent
    assert '"limit":30' in sent
    assert result.provider == "dataforseo"
    assert result.estimated is True
    assert len(result.rows) == 2
    keyword_row = next(row for row in result.rows if row.keyword == "seo tools")
    assert keyword_row.competitor_domain == "rival.example"
    assert keyword_row.competitor_position == 3
    assert keyword_row.own_position == 8
    assert keyword_row.search_volume == 1200
    assert keyword_row.url == "https://rival.example/tools"
    partial_row = next(row for row in result.rows if row.keyword == "bad row")
    assert partial_row.competitor_position is None
    assert partial_row.search_volume is None
    assert result.fetched_at is not None


@pytest.mark.asyncio
async def test_dataforseo_backlink_summary_and_referring_domains():
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=True) as mock:
            summary_route = mock.post(f"{ROOT}/backlinks/summary/live").mock(
                return_value=httpx.Response(200, json={
                    "status_code": 20000,
                    "tasks": [{
                        "status_code": 20000,
                        "result": [{
                            "backlinks": "301",
                            "referring_domains": 42,
                        }],
                    }],
                })
            )
            referring_route = mock.post(
                f"{ROOT}/backlinks/referring_domains/live"
            ).mock(return_value=httpx.Response(200, json={
                "status_code": 20000,
                "tasks": [{
                    "status_code": 20000,
                    "result": [{
                        "items": [
                            {"domain": "z.example", "backlinks": 9, "rank": 50},
                            {"domain": "a.example", "backlinks": "20", "rank": "80"},
                            {"domain": "bad row", "backlinks": "nope", "rank": None},
                            {"backlinks": 7},
                        ]
                    }],
                }],
            }))
            provider = DataForSEOProvider("login", "password", client)
            summary = await provider.backlink_summary("own.example")
            domains = await provider.referring_domains("own.example", limit=3)

    assert summary_route.called and referring_route.called
    assert summary == BacklinkSummary(
        provider="dataforseo",
        domain="own.example",
        backlinks=301,
        referring_domains=42,
        estimated=True,
        fetched_at=summary.fetched_at,
    )
    assert [(row.domain, row.backlinks, row.rank) for row in domains] == [
        ("a.example", 20, 80),
        ("z.example", 9, 50),
        ("bad row", None, None),
    ]


@pytest.mark.asyncio
async def test_dataforseo_surfaces_task_errors_and_rejects_invalid_limits():
    async with httpx.AsyncClient() as client:
        provider = DataForSEOProvider("login", "password", client)
        with pytest.raises(ValueError, match="limit"):
            await provider.keyword_gap("own.example", ["rival.example"], limit=0)
        with pytest.raises(ValueError, match="limit"):
            await provider.referring_domains("own.example", limit=0)
        with respx.mock(assert_all_called=True) as mock:
            mock.post(f"{ROOT}/backlinks/summary/live").mock(
                return_value=httpx.Response(200, json={
                    "status_code": 20000,
                    "tasks": [{
                        "status_code": 40501,
                        "status_message": "Invalid target",
                    }],
                })
            )
            with pytest.raises(DataForSEOError, match="40501"):
                await provider.backlink_summary("bad.example")


def test_compare_backlinks_orders_known_estimates_before_unknown():
    result = compare_backlinks([
        BacklinkSummary("dataforseo", "z.example", None, None),
        BacklinkSummary("dataforseo", "b.example", 5, 2),
        BacklinkSummary("dataforseo", "a.example", 10, 4),
    ])
    assert [row["domain"] for row in result] == ["a.example", "b.example", "z.example"]
    assert result[-1]["backlinks"] is None
