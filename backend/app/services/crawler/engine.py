"""Bounded breadth-first crawler.

Storage is abstracted behind ``CrawlSink`` so the engine is testable without a database;
the worker provides a DB-backed sink that persists pages, links and progress and reports
cancellation.
"""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.services.crawler.fetcher import FetchPolicy, FetchResult, build_client, safe_get
from app.services.crawler.frontier import BloomFilter, detect_crawl_trap
from app.services.crawler.parser import ParsedPage, parse_html, parse_robots, parse_sitemap, robots_sitemaps
from app.services.crawler.urls import host_of, normalize_url, redact_url

log = logging.getLogger(__name__)
MAX_SITEMAP_FILES = 20
MAX_SITEMAP_URLS = 50_000


@dataclass
class CrawlConfig:
    max_pages: int = 500
    max_depth: int = 5
    concurrency: int = 4
    request_delay_ms: int = 500
    timeout_s: float = 15.0


@dataclass
class PageRecord:
    url: str
    normalized_url: str
    depth: int
    fetch: FetchResult
    parsed: ParsedPage | None
    x_robots_tag: str | None
    internal_links: list = field(default_factory=list)
    external_links: list = field(default_factory=list)


@dataclass
class CrawlOutcome:
    pages_discovered: int = 0
    pages_crawled: int = 0
    pages_failed: int = 0
    sitemap_found: bool = False
    sitemap_urls: list[str] = field(default_factory=list)
    robots_blocked_urls: list[str] = field(default_factory=list)
    truncated: bool = False
    cancelled: bool = False
    robots_txt: str | None = None
    llms_txt_found: bool = False


class CrawlSink(Protocol):
    async def save_page(self, record: PageRecord) -> None: ...
    async def progress(self, discovered: int, crawled: int, failed: int) -> None: ...
    async def should_cancel(self) -> bool: ...


class HostRateLimiter:
    """Enforces a minimum delay between request starts per host.

    With a shared TokenBucket (Redis) the limit is global across all tenants, jobs and worker
    processes, so the platform can never multiply load on one host.
    """

    def __init__(self, delay_s: float, bucket=None):
        self.delay_s = delay_s
        self.bucket = bucket if bucket is not None and getattr(bucket, "shared", False) else None
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, host: str) -> None:
        if self.bucket is not None and self.delay_s > 0:
            from app.infra.ratelimit import host_key
            await self.bucket.acquire(host_key(host), rate=1 / self.delay_s, burst=1)
            return
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(host, 0.0))
            self._next[host] = start + self.delay_s
        if start > now:
            await asyncio.sleep(start - now)


class Crawler:
    def __init__(self, start_url: str, allowed_hosts: list[str], config: CrawlConfig, policy: FetchPolicy,
                 sink: CrawlSink, transport: httpx.AsyncBaseTransport | None = None, bucket=None):
        self.start_url = normalize_url(start_url) or start_url
        self.allowed = {h.lower() for h in allowed_hosts}
        self.config = config
        self.policy = policy
        self.sink = sink
        self.transport = transport
        self.limiter = HostRateLimiter(config.request_delay_ms / 1000, bucket)
        self.robots: dict[str, object] = {}
        self.outcome = CrawlOutcome()
        self._skip = [re.compile(p, re.I) for p in policy.skip_path_patterns]

    def _skipped(self, url: str) -> bool:
        path = urlsplit(url).path
        return any(p.search(path) for p in self._skip)

    def _internal(self, url: str) -> bool:
        return host_of(url) in self.allowed

    async def _robots_for(self, client: httpx.AsyncClient, url: str):
        p = urlsplit(url)
        origin = urlunsplit((p.scheme, p.netloc, "", "", ""))
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            await self.limiter.wait(host_of(url))
            res = await safe_get(client, robots_url, self.policy, accept_non_html=True)
            text = res.body.decode("utf-8", "replace") if res.status_code == 200 else ""
            # 4xx → no restrictions; 5xx/errors → be conservative and disallow all (Google's behaviour).
            if res.status_code and 500 <= res.status_code or (res.error and not res.blocked):
                text = "User-agent: *\nDisallow: /"
            self.robots[origin] = (parse_robots(text, robots_url), robots_sitemaps(text) if res.status_code == 200 else [])
            if self.outcome.robots_txt is None and res.status_code == 200:
                self.outcome.robots_txt = text
        return self.robots[origin]

    def _allowed_by_robots(self, rp, url: str) -> bool:
        return rp.can_fetch(self.policy.user_agent, url)

    async def _load_sitemaps(self, client: httpx.AsyncClient, declared: list[str]) -> list[str]:
        origin = urlunsplit(urlsplit(self.start_url)[:2] + ("", "", ""))
        queue = deque(dict.fromkeys([normalize_url(u) for u in declared] + [origin + "/sitemap.xml"]))
        seen, urls = set(), []
        while queue and len(seen) < MAX_SITEMAP_FILES and len(urls) < MAX_SITEMAP_URLS:
            sm = queue.popleft()
            if not sm or sm in seen or not self._internal(sm):
                continue
            seen.add(sm)
            await self.limiter.wait(host_of(sm))
            res = await safe_get(client, sm, self.policy, accept_non_html=True)
            if res.status_code != 200 or not res.body:
                continue
            pages, children = parse_sitemap(res.body)
            if pages or children:
                self.outcome.sitemap_found = True
            for u in pages:
                n = normalize_url(u)
                if n and self._internal(n):
                    urls.append(n)
            queue.extend(normalize_url(c) for c in children)
        return list(dict.fromkeys(urls))[:MAX_SITEMAP_URLS]

    async def run(self) -> CrawlOutcome:
        async with build_client(self.policy, self.transport) as client:
            rp, declared = await self._robots_for(client, self.start_url)
            sitemap_urls = await self._load_sitemaps(client, declared)
            self.outcome.sitemap_urls = sitemap_urls

            origin = urlunsplit(urlsplit(self.start_url)[:2] + ("", "", ""))
            llms_url = origin + "/llms.txt"
            try:
                llms_res = await safe_get(client, llms_url, self.policy, accept_non_html=True)
                if llms_res.status_code == 200 and llms_res.body and len(llms_res.body.strip()) > 0:
                    self.outcome.llms_txt_found = True
            except Exception:
                pass

            asset_cache: dict[str, bool] = {}
            minified_pat = re.compile(r"(\.min\.|-min\.|\.prod\.|\.bundle\.min\.|[-.][a-f0-9]{8,}\.(js|css))", re.I)

            async def is_asset_unminified(asset_url: str) -> bool:
                if asset_url in asset_cache:
                    return asset_cache[asset_url]
                if minified_pat.search(asset_url):
                    asset_cache[asset_url] = False
                    return False
                if not self._internal(asset_url):
                    asset_cache[asset_url] = False
                    return False
                try:
                    await self.limiter.wait(host_of(asset_url))
                    ares = await safe_get(client, asset_url, self.policy, accept_non_html=True)
                    if ares.status_code == 200 and ares.body:
                        from app.services.crawler.parser import is_unminified_code
                        text = ares.body.decode("utf-8", "ignore")
                        unmin = is_unminified_code(text)
                        asset_cache[asset_url] = unmin
                        return unmin
                except Exception:
                    pass
                asset_cache[asset_url] = False
                return False

            seen: set[str] = {self.start_url}
            bloom = BloomFilter(capacity=max(10_000, self.config.max_pages * 10))
            bloom.add(self.start_url)
            frontier: deque[tuple[str, int]] = deque([(self.start_url, 0)])
            for u in sitemap_urls:  # sitemap URLs are discovery seeds at depth 1 unless linked earlier
                if u not in seen and not detect_crawl_trap(u):
                    seen.add(u)
                    bloom.add(u)
                    frontier.append((u, 1))
            blocked: set[str] = set()
            in_flight: set[asyncio.Task] = set()
            self.outcome.pages_discovered = len(seen)

            async def process(url: str, depth: int) -> None:
                await self.limiter.wait(host_of(url))
                res = await safe_get(client, url, self.policy)
                from app.infra import observability as obs
                obs.CRAWL_PAGES.inc(role="audit", outcome="error" if (res.error or (res.status_code and res.status_code >= 400)) else "ok")
                parsed = parse_html(res.body, res.final_url or url, res.content_type) if res.is_html and res.body else None
                if parsed:
                    for res_url in (parsed.stylesheets + parsed.scripts):
                        if await is_asset_unminified(res_url):
                            parsed.unminified_resources.append(res_url)
                rec = PageRecord(url=url, normalized_url=url, depth=depth, fetch=res, parsed=parsed,
                                 x_robots_tag=res.headers.get("x-robots-tag"))
                if parsed:
                    for link in parsed.links:
                        (rec.internal_links if self._internal(link.href) else rec.external_links).append(link)
                    nofollow_page = "nofollow" in (parsed.meta_robots or "")
                    if depth < self.config.max_depth and not nofollow_page:
                        for link in rec.internal_links:
                            if (link.href not in seen and "nofollow" not in (link.rel or "").lower()
                                    and not detect_crawl_trap(link.href)):
                                seen.add(link.href)
                                bloom.add(link.href)
                                frontier.append((link.href, depth + 1))
                # Redirect targets inside the allowlist are discovered too (they get their own page row).
                if res.final_url and res.final_url != url and res.final_url not in seen and self._internal(res.final_url):
                    seen.add(res.final_url)
                    frontier.append((res.final_url, depth))
                self.outcome.pages_crawled += 1
                if res.error or (res.status_code and res.status_code >= 400):
                    self.outcome.pages_failed += 1
                await self.sink.save_page(rec)

            crawled = 0
            while frontier or in_flight:
                if await self.sink.should_cancel():
                    self.outcome.cancelled = True
                    break
                while frontier and len(in_flight) < self.config.concurrency:
                    if crawled >= self.config.max_pages:
                        self.outcome.truncated = True
                        frontier.clear()
                        break
                    url, depth = frontier.popleft()
                    if self._skipped(url):
                        continue
                    origin_rp, _ = await self._robots_for(client, url)
                    if not self._allowed_by_robots(origin_rp, url):
                        blocked.add(url)
                        continue
                    crawled += 1
                    in_flight.add(asyncio.create_task(process(url, depth)))
                if not in_flight:
                    continue
                done, in_flight = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if exc := t.exception():
                        log.warning("page task failed: %s", type(exc).__name__)
                        self.outcome.pages_failed += 1
                self.outcome.pages_discovered = len(seen)
                await self.sink.progress(self.outcome.pages_discovered, self.outcome.pages_crawled, self.outcome.pages_failed)
            for t in in_flight:
                t.cancel()
            self.outcome.robots_blocked_urls = sorted(blocked)
            log.info("crawl finished %s crawled=%d", redact_url(self.start_url), self.outcome.pages_crawled)
            return self.outcome
