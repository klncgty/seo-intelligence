"""Free data sources: Bing Webmaster API (tenant's own data) and Google Ads Keyword Planner (search volume).

Bing: per-organization API key (Bing Webmaster Tools → Settings → API access), stored encrypted.
Google Ads: free Ads account + approved developer token + OAuth refresh token with the `adwords` scope.
Volumes from Keyword Planner are rounded/bucketed by Google (precision "rounded"); bids are NOT reported as CPC.
"""

import re
from datetime import UTC, datetime

import httpx

from app.core.config import get_settings
from app.normalize.locale import LOCALES, SearchContext
from app.providers.base import KeywordMetric, Lineage, ProviderError, ProviderTransientError

BING = "https://ssl.bing.com/webmaster/api.svc/json"
_MS_DATE = re.compile(r"/Date\((-?\d+)")


def parse_ms_date(v: str | None) -> str | None:
    m = _MS_DATE.search(v or "")
    return datetime.fromtimestamp(int(m.group(1)) / 1000, UTC).date().isoformat() if m else None


async def bing_call(api_key: str, method: str, client: httpx.AsyncClient | None = None, **params):
    own = client is None
    c = client or httpx.AsyncClient(timeout=60, trust_env=False)
    try:
        r = await c.get(f"{BING}/{method}", params={"apikey": api_key, **params})
    except httpx.TransportError as exc:
        raise ProviderTransientError(type(exc).__name__) from exc
    finally:
        if own:
            await c.aclose()
    if r.status_code == 429 or r.status_code >= 500:
        raise ProviderTransientError(f"Bing HTTP {r.status_code}")
    if r.status_code != 200:
        raise ProviderError(f"Bing HTTP {r.status_code}")
    return r.json().get("d")


async def bing_query_stats(api_key: str, site_url: str, client=None) -> list[dict]:
    rows = await bing_call(api_key, "GetQueryStats", client, siteUrl=site_url) or []
    return [{"date": parse_ms_date(r.get("Date")), "query": r.get("Query") or "", "clicks": int(r.get("Clicks") or 0),
             "impressions": int(r.get("Impressions") or 0), "avg_impression_position": r.get("AvgImpressionPosition"),
             "avg_click_position": r.get("AvgClickPosition")} for r in rows if r.get("Query")]


async def bing_inbound_links(api_key: str, site_url: str, client=None, max_targets: int = 50) -> list[dict]:
    """Sample of inbound links Bing reports for the tenant's site: GetLinkCounts → GetUrlLinks per target URL."""
    counts = await bing_call(api_key, "GetLinkCounts", client, siteUrl=site_url, page=0) or {}
    links = []
    for item in (counts.get("Links") or [])[:max_targets]:
        url = item.get("Url")
        if not url:
            continue
        details = await bing_call(api_key, "GetUrlLinks", client, siteUrl=site_url, link=url, page=0) or {}
        for det in details.get("Details") or []:
            if det.get("Url"):
                links.append({"source_url": det["Url"], "target_url": url, "anchor": det.get("AnchorText") or "", "rel": "",
                              "link_position": "unknown"})
    return links


# ---------------------------------------------------------------- Google Ads Keyword Planner

LANGUAGE_CONSTANTS = {"tr": 1037, "en": 1000, "de": 1001}
GEO_CONSTANTS = {"TR": 2792, "US": 2840, "GB": 2826, "DE": 2276}
MONTHS = {m: i for i, m in enumerate(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
                                       "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], start=1)}


class GoogleAdsKeywordPlanner:
    name = "google_ads"
    capabilities = ("keyword_metrics", "keyword_ideas")
    unit_cost_usd = {"keyword_metrics": 0.0, "keyword_ideas": 0.0}  # free; subject to API quotas
    rate_per_s = 1.0
    supported_databases = tuple(LOCALES)

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.google_ads_developer_token and s.google_ads_customer_id and s.google_ads_refresh_token
                    and s.google_client_id and s.google_client_secret)

    async def _token(self, c: httpx.AsyncClient) -> str:
        from app.services.integrations.google_oauth import refresh_access_token
        s = get_settings()
        return (await refresh_access_token(c, s.google_client_id, s.google_client_secret, s.google_ads_refresh_token)).access_token

    async def _post(self, method: str, body: dict) -> dict:
        s = get_settings()
        own = self._client is None
        c = self._client or httpx.AsyncClient(timeout=60, trust_env=False)
        try:
            headers = {"Authorization": f"Bearer {await self._token(c)}", "developer-token": s.google_ads_developer_token}
            if s.google_ads_login_customer_id:
                headers["login-customer-id"] = s.google_ads_login_customer_id
            r = await c.post(f"https://googleads.googleapis.com/{s.google_ads_api_version}/customers/{s.google_ads_customer_id}:{method}",
                             json=body, headers=headers)
        finally:
            if own:
                await c.aclose()
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderTransientError(f"Google Ads HTTP {r.status_code}")
        if r.status_code != 200:
            raise ProviderError(f"Google Ads HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _targeting(ctx: SearchContext) -> dict:
        return {"language": f"languageConstants/{LANGUAGE_CONSTANTS.get(ctx.locale.language, 1000)}",
                "geoTargetConstants": [f"geoTargetConstants/{GEO_CONSTANTS[ctx.locale.country]}"],
                "keywordPlanNetwork": "GOOGLE_SEARCH"}

    def _metric(self, ctx: SearchContext, text: str, m: dict) -> KeywordMetric:
        monthly = sorted((f"{x['year']}-{MONTHS.get(x.get('month'), 0):02d}", int(x["monthlySearches"]) if x.get("monthlySearches") is not None else None)
                         for x in m.get("monthlySearchVolumes") or [] if x.get("year"))
        vol = m.get("avgMonthlySearches")
        ci = m.get("competitionIndex")
        return KeywordMetric(text, ctx.database, int(vol) if vol is not None else None, "rounded" if vol is not None else "unknown",
                             monthly, None, None, int(ci) / 100 if ci is not None else None,
                             Lineage("PROVIDER", self.name, None, datetime.now(UTC)))

    async def keyword_metrics(self, ctx: SearchContext, keywords: list[str]) -> list[KeywordMetric]:
        data = await self._post("generateKeywordHistoricalMetrics", {"keywords": keywords[:1000], **self._targeting(ctx)})
        return [self._metric(ctx, r.get("text"), r.get("keywordMetrics") or {}) for r in data.get("results", []) if r.get("text")]

    async def keyword_ideas(self, ctx: SearchContext, seed: str, limit: int = 100) -> list[KeywordMetric]:
        body = {**self._targeting(ctx), "keywordSeed": {"keywords": [seed]}, "pageSize": min(limit, 1000)}
        data = await self._post("generateKeywordIdeas", body)
        return [self._metric(ctx, r.get("text"), r.get("keywordIdeaMetrics") or {}) for r in data.get("results", []) if r.get("text")][:limit]
