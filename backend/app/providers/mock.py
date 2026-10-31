"""Deterministic mock provider — ONLY for tests and DEMO-labelled organizations.

All output is flagged is_synthetic=True / source_class DEMO; the registry refuses to hand it to
non-demo organizations, so mock numbers can never appear in production views.
"""

import hashlib
from datetime import UTC, datetime

from app.normalize.domains import registrable_domain
from app.normalize.locale import LOCALES, SearchContext
from app.providers.base import KeywordMetric, Lineage, RankedKeyword, SerpItem, SerpResult


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


class MockProvider:
    name = "mock"
    capabilities = ("serp", "keyword_metrics", "keyword_ideas", "ranked_keywords", "backlinks")
    unit_cost_usd = {"serp": 0.002, "keyword_metrics": 0.0005, "keyword_ideas": 0.01, "ranked_keywords": 0.02, "backlinks": 0.02}
    rate_per_s = 50.0
    supported_databases = tuple(LOCALES)

    def __init__(self, domains: list[str] | None = None, fail_times: int = 0):
        self.domains = domains or ["example.com.tr", "rakip1.com.tr", "rakip2.com", "wiki.example.org", "shop.example.net"]
        self.fail_times = fail_times
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    def _lineage(self, cost: float) -> Lineage:
        return Lineage("DEMO", self.name, f"mock-{self.calls}", datetime.now(UTC), cost_usd=cost, is_synthetic=True)

    async def fetch_serp(self, ctx: SearchContext, keyword: str, depth: int = 100) -> SerpResult:
        from app.providers.base import ProviderTransientError
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderTransientError("mock transient failure")
        # deterministic subset per keyword (≥3 domains) so gaps exist, then deterministic order
        subset = [d for d in self.domains if _h(keyword, d) % 3 != 0] or self.domains[:3]
        if len(subset) < 3:
            subset = (subset + [d for d in self.domains if d not in subset])[:3]
        order = sorted(subset, key=lambda d: _h(keyword, ctx.database, ctx.device, d))
        items = []
        for i, d in enumerate(order[: min(depth, len(order))], start=1):
            url = f"https://{d}/{keyword.replace(' ', '-')}"
            items.append(SerpItem(i, i, "organic", url, registrable_domain(d) or d, f"{keyword} — {d}"))
        features = ["people_also_ask"] if _h(keyword) % 2 else ["featured_snippet"]
        return SerpResult(keyword, ctx, items, features, [f"{keyword} fiyat"], [f"{keyword} nedir?"],
                          self._lineage(self.unit_cost_usd["serp"]))

    async def keyword_metrics(self, ctx: SearchContext, keywords: list[str]) -> list[KeywordMetric]:
        self.calls += 1
        out = []
        for k in keywords:
            vol = (_h(k, ctx.database) % 5000) + 10
            out.append(KeywordMetric(k, ctx.database, vol, "exact", [("2026-09", vol)], round((_h(k) % 300) / 100, 2),
                                     ctx.locale.currency, (_h(k, "c") % 100) / 100,
                                     self._lineage(self.unit_cost_usd["keyword_metrics"] * len(keywords))))
        return out

    async def keyword_ideas(self, ctx: SearchContext, seed: str, limit: int = 100) -> list[KeywordMetric]:
        ideas = [f"{seed} {s}" for s in ("fiyat", "nedir", "en iyi", "yorum", "nasıl", "online", "ucuz")][:limit]
        return await self.keyword_metrics(ctx, ideas)

    async def ranked_keywords(self, ctx: SearchContext, domain: str, limit: int = 1000) -> list[RankedKeyword]:
        self.calls += 1
        kws = [f"anahtar {i}" for i in range(min(limit, 20))]
        return [RankedKeyword(k, ctx.database, (_h(k, domain) % 50) + 1, f"https://{domain}/{i}", (_h(k) % 1000) + 10,
                              self._lineage(self.unit_cost_usd["ranked_keywords"])) for i, k in enumerate(kws)]

    async def backlinks(self, domain: str, limit: int = 1000) -> list[dict]:
        self.calls += 1
        srcs = [f"blog{i}.example.org" for i in range(min(limit, 8))]
        return [{"source_url": f"https://{s}/post-{i}", "target_url": f"https://{domain}/" + ("eski-sayfa" if i == 0 else ""),
                 "anchor": domain.split(".")[0] if i % 2 else "kahve makinesi", "rel": "" if i % 3 else "nofollow",
                 "is_image": False, "first_seen": "2026-09-01", "last_seen": "2026-10-20", "status": "live",
                 "link_position": "content", "lineage": {"provider_request_id": f"mock-{self.calls}", "cost_usd": 0.02, "is_synthetic": True}}
                for i, s in enumerate(srcs)]
