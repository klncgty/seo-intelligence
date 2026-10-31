"""DataForSEO v3 adapter (SERP, Google Ads search volume, Labs keyword ideas / ranked keywords).

Response shapes follow the public v3 docs; contract tests use sanitized fixtures of that shape.
NOT yet verified against a live account (no credentials) — see docs/data-licensing.md.
"""

from datetime import UTC, datetime

import httpx

from app.normalize.domains import registrable_domain
from app.normalize.locale import LOCALES, SearchContext
from app.providers.base import (
    KeywordMetric,
    Lineage,
    ProviderError,
    ProviderTransientError,
    RankedKeyword,
    SerpItem,
    SerpResult,
)

BASE = "https://api.dataforseo.com/v3"
LOCATION_CODES = {"TR": 2792, "US": 2840, "GB": 2826, "DE": 2276}


class DataForSEO:
    name = "dataforseo"
    capabilities = ("serp", "keyword_metrics", "keyword_ideas", "ranked_keywords", "backlinks")
    unit_cost_usd = {"serp": 0.002, "keyword_metrics": 0.05, "keyword_ideas": 0.01, "ranked_keywords": 0.01, "backlinks": 0.02}
    rate_per_s = 10.0
    supported_databases = tuple(LOCALES)

    def __init__(self, login: str, password: str, client: httpx.AsyncClient | None = None):
        self._auth = (login, password)
        self._client = client

    def is_configured(self) -> bool:
        return bool(self._auth[0] and self._auth[1])

    async def _post(self, path: str, task: dict) -> tuple[dict, str | None, float]:
        own = self._client is None
        client = self._client or httpx.AsyncClient(timeout=60, trust_env=False)
        try:
            r = await client.post(f"{BASE}{path}", json=[task], auth=self._auth)
        except httpx.TransportError as exc:
            raise ProviderTransientError(f"transport: {type(exc).__name__}") from exc
        finally:
            if own:
                await client.aclose()
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderTransientError(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ProviderError(f"HTTP {r.status_code}")
        body = r.json()
        tasks = body.get("tasks") or []
        if body.get("status_code") != 20000 or not tasks:
            code = body.get("status_code")
            raise (ProviderTransientError if code and code >= 50000 else ProviderError)(f"API status {code}: {body.get('status_message')}")
        t = tasks[0]
        if t.get("status_code") != 20000:
            code = t.get("status_code") or 0
            raise (ProviderTransientError if code >= 50000 else ProviderError)(f"task status {code}: {t.get('status_message')}")
        return t, t.get("id"), float(t.get("cost") or body.get("cost") or 0.0)

    @staticmethod
    def _loc(ctx: SearchContext) -> dict:
        return {"location_code": LOCATION_CODES[ctx.locale.country], "language_code": ctx.locale.language}

    def _lineage(self, req_id, cost) -> Lineage:
        return Lineage("PROVIDER", self.name, req_id, datetime.now(UTC), cost_usd=cost)

    async def fetch_serp(self, ctx: SearchContext, keyword: str, depth: int = 100) -> SerpResult:
        task = {"keyword": keyword, "device": ctx.device, "depth": depth, **self._loc(ctx)}
        if ctx.location:
            task["location_name" if not ctx.location.replace(",", "").replace(".", "").replace("-", "").isdigit()
                 else "location_coordinate"] = ctx.location
            task.pop("location_code", None)
        t, req_id, cost = await self._post("/serp/google/organic/live/advanced", task)
        res = (t.get("result") or [{}])[0] or {}
        items, paa, related = [], [], []
        for it in res.get("items") or []:
            typ = it.get("type") or "unknown"
            if typ == "people_also_ask":
                paa += [q.get("title") for q in it.get("items") or [] if q.get("title")]
            if typ == "related_searches":
                related += [q for q in it.get("items") or [] if isinstance(q, str)]
            url = it.get("url")
            cited = [registrable_domain(r.get("domain", "")) or r.get("domain") for r in it.get("references") or []
                     if r.get("domain")] if typ == "ai_overview" else []
            items.append(SerpItem(int(it.get("rank_group") or 0), int(it.get("rank_absolute") or 0), typ, url,
                                  registrable_domain(it.get("domain") or "") or it.get("domain"), it.get("title"), cited))
        return SerpResult(keyword, ctx, items, list(res.get("item_types") or []), related, paa, self._lineage(req_id, cost))

    @staticmethod
    def _monthly(ms) -> list[tuple[str, int | None]]:
        return sorted((f"{m['year']}-{int(m['month']):02d}", m.get("search_volume")) for m in ms or [] if m.get("year"))

    async def keyword_metrics(self, ctx: SearchContext, keywords: list[str]) -> list[KeywordMetric]:
        t, req_id, cost = await self._post("/keywords_data/google_ads/search_volume/live", {"keywords": keywords, **self._loc(ctx)})
        out = []
        for r in t.get("result") or []:
            comp = r.get("competition_index")
            out.append(KeywordMetric(r.get("keyword"), ctx.database, r.get("search_volume"),
                                     "rounded" if r.get("search_volume") is not None else "unknown",  # Ads volumes are bucketed
                                     self._monthly(r.get("monthly_searches")), r.get("cpc"), ctx.locale.currency
                                     if r.get("cpc") is not None else None, comp / 100 if comp is not None else None,
                                     self._lineage(req_id, cost)))
        return out

    async def keyword_ideas(self, ctx: SearchContext, seed: str, limit: int = 100) -> list[KeywordMetric]:
        t, req_id, cost = await self._post("/dataforseo_labs/google/keyword_ideas/live",
                                           {"keywords": [seed], "limit": limit, **self._loc(ctx)})
        out = []
        for it in ((t.get("result") or [{}])[0] or {}).get("items") or []:
            ki = it.get("keyword_info") or {}
            out.append(KeywordMetric(it.get("keyword"), ctx.database, ki.get("search_volume"),
                                     "rounded" if ki.get("search_volume") is not None else "unknown",
                                     self._monthly(ki.get("monthly_searches")), ki.get("cpc"),
                                     ctx.locale.currency if ki.get("cpc") is not None else None, ki.get("competition"),
                                     self._lineage(req_id, cost)))
        return out

    async def ranked_keywords(self, ctx: SearchContext, domain: str, limit: int = 1000) -> list[RankedKeyword]:
        t, req_id, cost = await self._post("/dataforseo_labs/google/ranked_keywords/live",
                                           {"target": domain, "limit": limit, **self._loc(ctx)})
        out = []
        for it in ((t.get("result") or [{}])[0] or {}).get("items") or []:
            kd = it.get("keyword_data") or {}
            serp = (it.get("ranked_serp_element") or {}).get("serp_item") or {}
            if not kd.get("keyword") or not serp.get("rank_group"):
                continue
            out.append(RankedKeyword(kd["keyword"], ctx.database, int(serp["rank_group"]), serp.get("url") or "",
                                     (kd.get("keyword_info") or {}).get("search_volume"), self._lineage(req_id, cost)))
        return out

    async def backlinks(self, domain: str, limit: int = 1000) -> list[dict]:
        t, req_id, cost = await self._post("/backlinks/backlinks/live", {"target": domain, "limit": limit, "mode": "as_is"})
        out = []
        for it in ((t.get("result") or [{}])[0] or {}).get("items") or []:
            if not it.get("url_from") or not it.get("url_to"):
                continue
            out.append({"source_url": it["url_from"], "target_url": it["url_to"], "anchor": it.get("anchor") or "",
                        "rel": "" if it.get("dofollow", True) else "nofollow", "is_image": it.get("item_type") == "image",
                        "first_seen": (it.get("first_seen") or "")[:10] or None, "last_seen": (it.get("last_seen") or "")[:10] or None,
                        "status": "lost" if it.get("is_lost") else "live", "link_position": "unknown",
                        "lineage": {"provider_request_id": req_id, "cost_usd": cost}})
        return out
