import asyncio
import json
import pathlib

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.models import Organization, ProviderCache, ProviderUsage
from app.normalize.locale import SearchContext
from app.providers import registry, runtime
from app.providers.base import BudgetExceeded, CircuitOpen, ProviderError, ProviderNotConfigured, ProviderTransientError
from app.providers.dataforseo import DataForSEO
from app.providers.mock import MockProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "dataforseo"
TR = SearchContext("tr-TR:google.com.tr")


@pytest.fixture(autouse=True)
def _reset():
    registry.clear_overrides()
    runtime.BREAKER.failures.clear()
    runtime.BREAKER.opened_at.clear()
    runtime.BUCKET._local.clear()
    yield
    registry.clear_overrides()


@respx.mock
def test_dataforseo_contract_serp_and_volume():
    respx.post("https://api.dataforseo.com/v3/serp/google/organic/live/advanced").respond(json=json.loads((FIX / "serp.json").read_text()))
    respx.post("https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live").respond(
        json=json.loads((FIX / "search_volume.json").read_text()))
    p = DataForSEO("login", "pw")
    serp = asyncio.run(p.fetch_serp(TR, "kahve makinesi"))
    organic = [i for i in serp.items if i.result_type == "organic"]
    assert [(i.rank_group, i.rank_absolute, i.domain) for i in organic] == [(1, 2, "example.com.tr"), (2, 4, "rakip.com")]
    ai = next(i for i in serp.items if i.result_type == "ai_overview")
    assert ai.cited_domains == ["example.com.tr", "rakip.com"]
    assert serp.people_also_ask == ["En iyi kahve makinesi hangisi?"] and serp.lineage.provider_request_id == "req-serp-1"
    assert serp.lineage.source_class == "PROVIDER" and serp.lineage.cost_usd == 0.002
    req = json.loads(respx.calls[0].request.content)[0]
    assert req["location_code"] == 2792 and req["language_code"] == "tr"
    vols = asyncio.run(p.keyword_metrics(TR, ["kahve makinesi", "nadir sorgu"]))
    assert vols[0].search_volume == 14800 and vols[0].monthly[-1] == ("2026-09", 14800) and vols[0].ads_competition == 0.87
    assert vols[1].search_volume is None and vols[1].volume_precision == "unknown"  # never fabricated


@respx.mock
def test_dataforseo_schema_change_and_errors_detected():
    respx.post("https://api.dataforseo.com/v3/serp/google/organic/live/advanced").mock(side_effect=[
        httpx.Response(200, json={"status_code": 40501, "status_message": "Invalid Field"}),
        httpx.Response(503), httpx.Response(200, json={"status_code": 20000, "tasks": [{"status_code": 50000}]})])
    p = DataForSEO("l", "p")
    with pytest.raises(ProviderError) as e:
        asyncio.run(p.fetch_serp(TR, "x"))
    assert not isinstance(e.value, ProviderTransientError)
    with pytest.raises(ProviderTransientError):
        asyncio.run(p.fetch_serp(TR, "x"))
    with pytest.raises(ProviderTransientError):
        asyncio.run(p.fetch_serp(TR, "x"))


def test_not_configured_without_keys(db):
    with pytest.raises(ProviderNotConfigured):
        registry.get("serp", db, 1)  # local org, no credentials → no mock either
    assert registry.status()[0]["configured"] is False and registry.status()[0]["setup"]


def test_mock_only_for_demo_orgs(db):
    db.add(Organization(id=50, name="Demo", plan="local", is_demo=True, suspended=False, cross_tenant_opt_in=False))
    db.commit()
    assert isinstance(registry.get("serp", db, 50), MockProvider)
    with pytest.raises(ProviderNotConfigured):
        registry.get("serp", db, 1)


def test_runtime_cache_budget_retry_circuit(db, settings):
    settings(provider_daily_budget_usd=0.005, provider_monthly_budget_usd=1.0)
    p = MockProvider(fail_times=1)
    call = lambda kw: runtime.call(db, p, "serp", {"kw": kw, "db": TR.database},  # noqa: E731
                                   lambda: p.fetch_serp(TR, kw), org_id=1, base_delay=0)
    first = asyncio.run(call("kahve"))  # one transient failure → retried
    assert first["cached"] is False and p.calls == 2 and first["data"]["items"]
    again = asyncio.run(call("kahve"))
    assert again["cached"] is True and again["cost_usd"] == 0.0 and p.calls == 2  # not paid twice
    asyncio.run(call("çay"))
    with pytest.raises(BudgetExceeded):  # 0.002 * 3 > 0.005 daily cap
        asyncio.run(call("su"))
    assert db.scalar(select(func.count()).select_from(ProviderCache)) == 2
    assert db.scalar(select(func.count()).select_from(ProviderUsage).where(ProviderUsage.cached.is_(True))) == 1
    b = runtime.budget_status(db)
    assert b["daily_spend_usd"] == pytest.approx(0.004)
    settings(provider_daily_budget_usd=100)
    bad = MockProvider(fail_times=100)
    for i in range(runtime.FAILURE_THRESHOLD):
        with pytest.raises(ProviderTransientError):
            asyncio.run(runtime.call(db, bad, "serp", {"i": i}, lambda: bad.fetch_serp(TR, "x"), max_retries=0, base_delay=0))
    with pytest.raises(CircuitOpen):
        asyncio.run(runtime.call(db, bad, "serp", {"i": "next"}, lambda: bad.fetch_serp(TR, "x")))


def test_mock_output_is_flagged_synthetic():
    serp = asyncio.run(MockProvider().fetch_serp(TR, "kahve"))
    assert serp.lineage.is_synthetic and serp.lineage.source_class == "DEMO"
