"""Site operations: domain verification, log analysis, migration assistant, CWV (CrUX/PSI), GSC URL Inspection.

All outbound fetches to the tenant's site go through the SSRF-safe fetcher with the site allowlist.
"""

import asyncio
import csv
import io
import re
import secrets
import socket
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import BotHitDaily, CrawlPage, CrawlRun, Site
from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
from app.services.crawler.urls import host_of, normalize_url

VERIFY_META = "seo-growth-verification"
VERIFY_TXT_PREFIX = "seo-growth-verification="


# ---------------------------------------------------------------- domain verification

def start_verification(db: Session, site: Site) -> dict:
    if not site.verification_token:
        site.verification_token = secrets.token_hex(16)
        db.commit()
    t = site.verification_token
    host = host_of(site.base_url)
    return {"token": t, "methods": {
        "dns_txt": f'Add a TXT record on {host}: "{VERIFY_TXT_PREFIX}{t}"',
        "html_meta": f'Add to the homepage <head>: <meta name="{VERIFY_META}" content="{t}">',
        "file": f"Serve {site.base_url.rstrip('/')}/{VERIFY_META}-{t}.txt containing exactly: {t}",
        "gsc": "Map a Search Console property you own (gsc_property) and connect Google; ownership is checked via the API"},
        "unverified_limits": {"max_pages": get_settings().unverified_max_pages, "max_depth": get_settings().unverified_max_depth,
                              "js_rendering": False, "log_upload": False}}


def _txt_records(host: str) -> list[str]:
    import dns.resolver
    try:
        ans = dns.resolver.resolve(host, "TXT", lifetime=5)
    except Exception:
        return []
    return [b"".join(r.strings).decode("utf-8", "replace") for r in ans]


async def check_verification(db: Session, site: Site, method: str, *, txt_lookup=_txt_records, transport=None,
                             gsc_sites: list[str] | None = None) -> dict:
    t = site.verification_token
    if not t:
        raise ValueError("start verification first")
    ok, detail = False, ""
    if method == "dns_txt":
        records = await asyncio.to_thread(txt_lookup, host_of(site.base_url))
        ok = f"{VERIFY_TXT_PREFIX}{t}" in records
        detail = f"{len(records)} TXT records checked"
    elif method in ("html_meta", "file"):
        s = get_settings()
        policy = FetchPolicy(allowed_hosts=list(site.allowed_hosts or [host_of(site.base_url)]), user_agent=s.crawler_user_agent,
                             timeout_s=15, max_bytes=1_000_000, allow_private=s.crawler_allow_private_networks)
        url = site.base_url if method == "html_meta" else f"{site.base_url.rstrip('/')}/{VERIFY_META}-{t}.txt"
        async with build_client(policy, transport) as c:
            res = await safe_get(c, url, policy, accept_non_html=True)
        body = res.body.decode("utf-8", "replace")
        if method == "html_meta":
            from lxml import html as lhtml
            doc = lhtml.fromstring(body or "<html/>")
            ok = t in [m.get("content", "") for m in doc.xpath(f"//meta[@name='{VERIFY_META}']")]
        else:
            ok = res.status_code == 200 and body.strip() == t
        detail = f"HTTP {res.status_code}" if res.status_code else (res.error or "fetch failed")
    elif method == "gsc":
        if gsc_sites is None:
            from app.services import google_service
            props = await google_service.list_properties(db)
            gsc_sites = [p["site_url"] for p in props["search_console"] if p.get("permission_level") in ("siteOwner", "siteFullUser")]
        ok = bool(site.gsc_property) and site.gsc_property in gsc_sites
        detail = "property ownership checked via Search Console API"
    else:
        raise ValueError("method must be dns_txt, html_meta, file or gsc")
    if ok:
        site.verified_at, site.verification_method = datetime.now(UTC), method
        db.commit()
    return {"verified": ok, "method": method, "detail": detail}


# ---------------------------------------------------------------- log analysis

LOG_RE = re.compile(r'^(?P<ip>[0-9a-fA-F:.]+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
                    r'(?P<status>\d{3}) \S+ "[^"]*" "(?P<ua>[^"]*)"')
BOTS = {"googlebot": ("Googlebot", ("googlebot.com", "google.com", "googleusercontent.com")),
        "bingbot": ("bingbot", ("search.msn.com",)), "yandex": ("YandexBot", ("yandex.ru", "yandex.net", "yandex.com"))}
MAX_LOG_BYTES = 200 * 1024 * 1024


def _rdns(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0].lower()
    except OSError:
        return None


def _forward(host: str) -> set[str]:
    try:
        return {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


def verify_bot(ip: str, bot: str, rdns=_rdns, forward=_forward, cache: dict | None = None) -> bool:
    """Reverse DNS must end in the bot's domain AND forward-resolve back to the same IP."""
    key = (ip, bot)
    if cache is not None and key in cache:
        return cache[key]
    host = rdns(ip)
    ok = bool(host) and host.rstrip(".").endswith(BOTS[bot][1]) and ip in forward(host)
    if cache is not None:
        cache[key] = ok
    return ok


def ingest_log(db: Session, site: Site, lines, *, rdns=_rdns, forward=_forward) -> dict:
    """Stream-parse combined-format logs. Only bot requests are aggregated; other visitors' IPs are discarded."""
    agg: Counter = Counter()
    cache: dict = {}
    parsed = skipped = humans = 0
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        m = LOG_RE.match(line.strip())
        if not m:
            skipped += 1
            continue
        parsed += 1
        ua = m["ua"]
        bot = next((b for b, (token, _) in BOTS.items() if token.lower() in ua.lower()), None)
        if bot is None:
            humans += 1
            continue  # non-bot traffic: not stored at all
        try:
            day = datetime.strptime(m["ts"].split()[0], "%d/%b/%Y:%H:%M:%S").date()
        except ValueError:
            skipped += 1
            continue
        verified = verify_bot(m["ip"], bot, rdns, forward, cache)
        agg[(day, bot, verified, m["path"].split("#")[0][:2000], int(m["status"]))] += 1
    rows = [{"site_id": site.id, "date": d, "bot": b, "verified": v, "path": p, "status": st, "hits": n}
            for (d, b, v, p, st), n in agg.items()]
    for i in range(0, len(rows), 1000):
        stmt = insert(BotHitDaily).values(rows[i:i + 1000])
        db.execute(stmt.on_conflict_do_update(constraint="uq_bot_hit", set_={"hits": BotHitDaily.hits + stmt.excluded.hits}))
    db.commit()
    return {"lines_parsed": parsed, "lines_skipped": skipped, "non_bot_lines_discarded": humans, "bot_rows": len(rows),
            "note": "re-uploading the same log adds hits again; upload each log period once"}


def log_report(db: Session, site: Site, days: int = 30) -> dict:
    since = date.today() - timedelta(days=days)
    rows = db.execute(select(BotHitDaily).where(BotHitDaily.site_id == site.id, BotHitDaily.date >= since)).scalars().all()
    if not rows:
        return {"available": False, "reason": "no logs uploaded"}
    g = [r for r in rows if r.bot == "googlebot" and r.verified]
    by_day = defaultdict(int)
    statuses = Counter()
    paths = Counter()
    for r in g:
        by_day[r.date.isoformat()] += r.hits
        statuses[r.status] += r.hits
        paths[r.path] += r.hits
    crawl = db.scalar(select(CrawlRun).where(CrawlRun.site_id == site.id, CrawlRun.competitor_id.is_(None),
                                             CrawlRun.status == "completed").order_by(CrawlRun.id.desc()).limit(1))
    important, orphans = [], []
    if crawl:
        from urllib.parse import urlsplit
        known = {urlsplit(u).path or "/" for u in db.scalars(select(CrawlPage.normalized_url).where(CrawlPage.crawl_run_id == crawl.id))}
        sitemap_paths = {urlsplit(u).path or "/" for u in crawl.sitemap_urls or []}
        crawled_paths = {p.split("?")[0] for p in paths}
        important = sorted(sitemap_paths - crawled_paths)[:200]
        orphans = sorted(p for p in crawled_paths if p not in known and statuses)[:200]
    spoofed = sum(r.hits for r in rows if r.bot == "googlebot" and not r.verified)
    return {"available": True, "window_days": days, "googlebot_verified_hits_by_day": dict(sorted(by_day.items())),
            "status_codes_to_googlebot": dict(statuses), "top_crawled_paths": paths.most_common(50),
            "important_not_crawled": important, "crawled_not_in_site_graph": orphans,
            "unverified_googlebot_hits": spoofed, "source_class": "FIRST_PARTY"}


# ---------------------------------------------------------------- migration assistant

def parse_redirect_map(text: str, limit: int = 1000) -> list[tuple[str, str]]:
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or row[0].strip().lower() in ("from", "old", "source"):
            continue
        a, b = normalize_url(row[0].strip()), normalize_url(row[1].strip())
        if a and b:
            out.append((a, b))
        if len(out) >= limit:
            break
    return out


async def validate_migration(site: Site, mapping: list[tuple[str, str]], transport=None, concurrency: int = 4) -> dict:
    s = get_settings()
    policy = FetchPolicy(allowed_hosts=list(site.allowed_hosts or []), user_agent=s.crawler_user_agent, timeout_s=15,
                         max_bytes=2_000_000, max_redirects=5, allow_private=s.crawler_allow_private_networks, html_only=True)
    sem = asyncio.Semaphore(concurrency)
    results = []
    async with build_client(policy, transport) as c:
        async def one(old, new):
            async with sem:
                res = await safe_get(c, old, policy)
            hops = res.redirect_chain
            issues = []
            if res.blocked:
                issues.append("blocked: host not in site allowlist / not allowed")
            elif not hops:
                issues.append("no redirect")
            else:
                if len(hops) > 1:
                    issues.append(f"{len(hops)} hops (expected 1)")
                if hops[0]["status_code"] not in (301, 308):
                    issues.append(f"first hop {hops[0]['status_code']} (expected 301/308)")
                if res.final_url != new:
                    issues.append("wrong target")
                if res.status_code != 200:
                    issues.append(f"target returned {res.status_code or res.error}")
            results.append({"from": old, "expected": new, "final_url": res.final_url, "final_status": res.status_code,
                            "chain": hops, "ok": not issues, "issues": issues})
        await asyncio.gather(*(one(a, b) for a, b in mapping))
    results.sort(key=lambda r: (r["ok"], r["from"]))
    return {"checked": len(results), "ok": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results),
            "results": results}


# ---------------------------------------------------------------- CWV (CrUX field / PSI lab)

CRUX = "https://chromeuxreport.googleapis.com/v1/records"
PSI = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


async def crux(target: str, kind: str = "origin", form_factor: str = "PHONE", history: bool = False,
               client: httpx.AsyncClient | None = None) -> dict:
    key = get_settings().crux_api_key
    if not key:
        return {"available": False, "reason": "not connected: set CRUX_API_KEY (Google Cloud API key with CrUX API enabled)"}
    own = client is None
    c = client or httpx.AsyncClient(timeout=30, trust_env=False)
    try:
        r = await c.post(f"{CRUX}:{'queryHistoryRecord' if history else 'queryRecord'}", params={"key": key},
                         json={kind: target, "formFactor": form_factor})
    finally:
        if own:
            await c.aclose()
    if r.status_code == 404:
        return {"available": False, "reason": "insufficient real-user traffic in CrUX for this target", "source_class": "FIRST_PARTY"}
    if r.status_code != 200:
        return {"available": False, "reason": f"CrUX API error {r.status_code}"}
    rec = r.json().get("record", {})
    metrics = {k: v.get("percentiles", {}).get("p75") if not history else v.get("percentilesTimeseries", {}).get("p75s")
               for k, v in rec.get("metrics", {}).items()}
    return {"available": True, "type": "field", "source": "CrUX", "source_class": "PROVIDER", "target": target,
            "form_factor": form_factor, "p75": metrics, "collection_period": rec.get("collectionPeriod") or rec.get("collectionPeriods")}


async def psi(url: str, strategy: str = "mobile", client: httpx.AsyncClient | None = None) -> dict:
    key = get_settings().crux_api_key
    if not key:
        return {"available": False, "reason": "not connected: set CRUX_API_KEY (PageSpeed Insights API enabled)"}
    own = client is None
    c = client or httpx.AsyncClient(timeout=90, trust_env=False)
    try:
        r = await c.get(PSI, params={"url": url, "key": key, "strategy": strategy, "category": "performance"})
    finally:
        if own:
            await c.aclose()
    if r.status_code != 200:
        return {"available": False, "reason": f"PSI error {r.status_code}"}
    lh = r.json().get("lighthouseResult", {})
    audits = lh.get("audits", {})
    pick = {"lcp_ms": "largest-contentful-paint", "cls": "cumulative-layout-shift", "tbt_ms": "total-blocking-time"}
    return {"available": True, "type": "lab", "source": "PageSpeed Insights (Lighthouse)", "source_class": "OBSERVED", "url": url,
            "strategy": strategy, "performance_score": (lh.get("categories", {}).get("performance", {}) or {}).get("score"),
            "metrics": {k: audits.get(v, {}).get("numericValue") for k, v in pick.items()},
            "lighthouse_version": lh.get("lighthouseVersion")}


def template_representatives(db: Session, site: Site, limit: int = 10) -> list[str]:
    """One URL per DOM template (highest internal PageRank) from the latest crawl — PSI runs only on these."""
    crawl = db.scalar(select(CrawlRun).where(CrawlRun.site_id == site.id, CrawlRun.competitor_id.is_(None),
                                             CrawlRun.status == "completed").order_by(CrawlRun.id.desc()).limit(1))
    if not crawl:
        return []
    rows = db.execute(select(CrawlPage.template_sig, CrawlPage.normalized_url, CrawlPage.internal_pagerank)
                      .where(CrawlPage.crawl_run_id == crawl.id, CrawlPage.template_sig.is_not(None), CrawlPage.status_code == 200)).all()
    best: dict[str, tuple] = {}
    sizes = Counter(r[0] for r in rows)
    for sig, url, pr in rows:
        if sig not in best or (pr or 0) > (best[sig][1] or 0):
            best[sig] = (url, pr)
    return [best[s][0] for s, _ in sizes.most_common(limit)]


# ---------------------------------------------------------------- GSC URL Inspection & Sitemaps

INSPECT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"


def inspections_today(db: Session, site_id: int) -> int:
    from app.models import ProviderUsage
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(ProviderUsage).where(
        ProviderUsage.provider == "gsc_url_inspection", ProviderUsage.capability == f"site:{site_id}",
        ProviderUsage.created_at >= start)) or 0


async def inspect_urls(db: Session, site: Site, urls: list[str], client: httpx.AsyncClient | None = None) -> dict:
    """URL Inspection within the daily per-property budget; flags Google-selected vs user canonical conflicts."""
    from app.models import ProviderUsage
    from app.services import google_service
    budget = get_settings().url_inspection_daily_quota - inspections_today(db, site.id)
    if not site.gsc_property:
        raise google_service.IntegrationNotConfigured("site has no gsc_property")
    todo = urls[:max(0, budget)]
    own = client is None
    c = client or httpx.AsyncClient(timeout=60, trust_env=False)
    results = []
    try:
        token = await google_service.access_token(db, c, site.org_id)
        for u in todo:
            r = await c.post(INSPECT, json={"inspectionUrl": u, "siteUrl": site.gsc_property},
                             headers={"Authorization": f"Bearer {token}"})
            db.add(ProviderUsage(provider="gsc_url_inspection", capability=f"site:{site.id}", org_id=site.org_id,
                                 cost_usd=0.0, ok=r.status_code == 200))
            if r.status_code != 200:
                results.append({"url": u, "error": f"HTTP {r.status_code}"})
                continue
            idx = (r.json().get("inspectionResult") or {}).get("indexStatusResult") or {}
            g, usr = idx.get("googleCanonical"), idx.get("userCanonical")
            results.append({"url": u, "verdict": idx.get("verdict"), "coverage_state": idx.get("coverageState"),
                            "indexing_state": idx.get("indexingState"), "last_crawl_time": idx.get("lastCrawlTime"),
                            "google_canonical": g, "user_canonical": usr,
                            "canonical_conflict": bool(g and usr and g != usr), "source_class": "FIRST_PARTY"})
        db.commit()
    finally:
        if own:
            await c.aclose()
    return {"inspected": len(todo), "skipped_over_budget": len(urls) - len(todo),
            "remaining_budget_today": budget - len(todo), "results": results}


async def gsc_sitemaps(db: Session, site: Site, client: httpx.AsyncClient | None = None) -> dict:
    from urllib.parse import quote

    from app.services import google_service
    if not site.gsc_property:
        raise google_service.IntegrationNotConfigured("site has no gsc_property")
    own = client is None
    c = client or httpx.AsyncClient(timeout=60, trust_env=False)
    try:
        token = await google_service.access_token(db, c, site.org_id)
        r = await c.get(f"https://www.googleapis.com/webmasters/v3/sites/{quote(site.gsc_property, safe='')}/sitemaps",
                        headers={"Authorization": f"Bearer {token}"})
    finally:
        if own:
            await c.aclose()
    if r.status_code != 200:
        return {"available": False, "reason": f"HTTP {r.status_code}"}
    return {"available": True, "source_class": "FIRST_PARTY", "sitemaps": [
        {"path": s.get("path"), "last_submitted": s.get("lastSubmitted"), "last_downloaded": s.get("lastDownloaded"),
         "is_pending": s.get("isPending"), "warnings": s.get("warnings"), "errors": s.get("errors")}
        for s in r.json().get("sitemap", [])]}
