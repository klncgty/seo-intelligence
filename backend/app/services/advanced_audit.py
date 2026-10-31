"""Advanced site audit (spec §10): runs after the deterministic base audit on a completed crawl.

- structured data rules (versioned `sd-rules-1`; required/recommended fields; no rich-result guarantee)
- near-duplicates (SimHash, 4×16-bit LSH bands, Hamming ≤ 3)
- internal PageRank (damping 0.85, 30 iterations) + internal link suggestions
- crawl-trap / facet explosion detection, image alt text
- template-level aggregation (DOM signature)
- site health score: 100 × (1 − mean_page(min(1, Σ severity_weight / 10))); weights critical 10, high 5, medium 2,
  low 0.5, info 0. A quality indicator — NOT a ranking score.
- crawl diff & regression alerts vs the previous completed crawl of the same site
"""

import json
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from lxml import html as lhtml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditFinding, CrawlPage, CrawlRun, PageLink, Site
from app.normalize.text import match_key
from app.services.crawler.parser import hamming

SD_RULES_VERSION = "sd-rules-1"
SD_RULES = {  # type → (required any-of groups, recommended)
    "Product": ([["name"], ["offers", "review", "aggregateRating"]], ["image", "brand", "sku", "description"]),
    "Article": ([["headline"]], ["image", "datePublished", "author"]),
    "NewsArticle": ([["headline"]], ["image", "datePublished", "author"]),
    "BlogPosting": ([["headline"]], ["image", "datePublished", "author"]),
    "Organization": ([["name"]], ["url", "logo"]),
    "LocalBusiness": ([["name"], ["address"]], ["telephone", "openingHours", "geo"]),
    "FAQPage": ([["mainEntity"]], []),
    "BreadcrumbList": ([["itemListElement"]], []),
    "Event": ([["name"], ["startDate"], ["location"]], ["image", "offers"]),
    "Recipe": ([["name"], ["image"]], ["recipeIngredient", "recipeInstructions"]),
}
SEVERITY_WEIGHT = {"critical": 10, "high": 5, "medium": 2, "low": 0.5, "info": 0}
MAX_EXTRACTORS, MAX_EXPR, REGEX_TIMEOUT_S = 10, 300, 0.1


# ---------------------------------------------------------------- custom extraction

def validate_extractors(extractors: list[dict]) -> list[dict]:
    import regex
    from lxml.cssselect import CSSSelector
    out = []
    for e in extractors[:MAX_EXTRACTORS]:
        name, typ, expr = str(e.get("name", ""))[:50], e.get("type"), str(e.get("expr", ""))
        if not name or typ not in ("css", "xpath", "regex") or not expr or len(expr) > MAX_EXPR:
            raise ValueError(f"invalid extractor {e!r}")
        try:
            if typ == "regex":
                regex.compile(expr)
            elif typ == "css":
                CSSSelector(expr)
            else:
                from lxml import etree
                etree.XPath(expr)
        except Exception as exc:  # regex.error / SelectorError / XPathSyntaxError → validation error
            raise ValueError(f"invalid {typ} expression for {name!r}: {type(exc).__name__}") from exc
        out.append({"name": name, "type": typ, "expr": expr})
    return out


def extract_custom(extractors: list[dict], body: bytes, parsed=None) -> dict:
    """CSS/XPath/regex extraction with cost limits; regex uses a per-call timeout (ReDoS protection)."""
    import regex
    from lxml.cssselect import CSSSelector
    if not extractors or not body:
        return {}
    text = body[:2_000_000].decode("utf-8", "replace")
    doc = None
    out = {}
    for e in extractors[:MAX_EXTRACTORS]:
        try:
            if e["type"] == "regex":
                vals = [m.group(0) for m in regex.finditer(e["expr"], text[:500_000], timeout=REGEX_TIMEOUT_S)][:5]
            else:
                doc = doc if doc is not None else lhtml.fromstring(text)
                nodes = CSSSelector(e["expr"])(doc) if e["type"] == "css" else doc.xpath(e["expr"])
                vals = [(n.text_content() if hasattr(n, "text_content") else str(n)).strip() for n in nodes[:5]]
            out[e["name"]] = [v[:300] for v in vals]
        except TimeoutError:
            out[e["name"]] = {"error": "regex timeout"}
        except Exception as exc:
            out[e["name"]] = {"error": type(exc).__name__}
    return out


# ---------------------------------------------------------------- rule helpers

def _f(rule_id, severity, url, title, description, evidence, remediation) -> dict:
    return {"rule_id": rule_id, "severity": severity, "url": url, "title": title, "description": description,
            "evidence": evidence, "remediation": remediation}


def structured_data_findings(pages: list[CrawlPage]) -> list[dict]:
    out = []
    for p in pages:
        for item in p.structured_data or []:
            if item.get("error"):
                out.append(_f("structured_data_invalid", "medium", p.normalized_url, "Invalid structured data block",
                              "A JSON-LD block could not be parsed; search engines will ignore it.",
                              {"format": item["format"], "error": item["error"], "rules": SD_RULES_VERSION},
                              "Fix the JSON syntax (validate with a structured data testing tool)."))
                continue
            rule = SD_RULES.get(item.get("type") or "")
            if not rule:
                continue
            fields = set(item.get("fields") or [])
            missing_req = [g for g in rule[0] if not fields & set(g)]
            missing_rec = [r for r in rule[1] if r not in fields]
            if missing_req:
                out.append(_f("structured_data_missing_required", "medium", p.normalized_url,
                              f"{item['type']} markup missing required properties",
                              "Required properties are missing, so this markup is not eligible for its rich result type. "
                              "Eligibility is never guaranteed even when complete.",
                              {"type": item["type"], "format": item["format"], "missing_any_of": missing_req, "rules": SD_RULES_VERSION},
                              "Add the missing properties with values that match visible page content."))
            elif missing_rec:
                out.append(_f("structured_data_missing_recommended", "info", p.normalized_url,
                              f"{item['type']} markup missing recommended properties", "Optional enhancement.",
                              {"type": item["type"], "missing": missing_rec, "rules": SD_RULES_VERSION},
                              "Consider adding recommended properties where accurate."))
    return out


def near_duplicates(pages: list[CrawlPage], max_distance: int = 3) -> list[dict]:
    cands = [p for p in pages if p.simhash is not None and p.status_code == 200 and (p.word_count or 0) >= 50]
    buckets: dict[tuple, list[CrawlPage]] = defaultdict(list)
    for p in cands:
        v = p.simhash & ((1 << 64) - 1)
        for band in range(4):
            buckets[(band, (v >> (band * 16)) & 0xFFFF)].append(p)
    pairs: dict[int, set[str]] = defaultdict(set)
    seen = set()
    for group in buckets.values():
        if len(group) > 200:  # pathological bucket (boilerplate-only pages) — skip to bound cost
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                key = (min(a.id, b.id), max(a.id, b.id))
                if key in seen or a.content_hash == b.content_hash and a.content_hash is None:
                    continue
                seen.add(key)
                if hamming(a.simhash, b.simhash) <= max_distance:
                    pairs[a.id].add(b.normalized_url)
                    pairs[b.id].add(a.normalized_url)
    by_id = {p.id: p for p in cands}
    return [_f("near_duplicate_content", "low", by_id[pid].normalized_url, "Near-duplicate content",
               "Page text is nearly identical to other crawled pages (SimHash). Duplicates split signals and waste crawl budget; "
               "this is a heuristic, review before acting.",
               {"similar_urls": sorted(urls)[:20], "method": "simhash64, hamming<=3"},
               "Consolidate, canonicalize to the primary version, or differentiate the content.")
            for pid, urls in sorted(pairs.items())]


def internal_pagerank(pages: list[CrawlPage], edges: list[tuple[str, str]], damping: float = 0.85, iters: int = 30) -> dict[str, float]:
    nodes = [p.normalized_url for p in pages if p.status_code == 200]
    idx = set(nodes)
    out_links: dict[str, set[str]] = defaultdict(set)
    for s, t in edges:
        if s in idx and t in idx and s != t:
            out_links[s].add(t)
    n = len(nodes)
    if n == 0:
        return {}
    pr = {u: 1 / n for u in nodes}
    for _ in range(iters):
        dangling = sum(pr[u] for u in nodes if not out_links[u])
        nxt = {u: (1 - damping) / n + damping * dangling / n for u in nodes}
        for s in nodes:
            if out_links[s]:
                share = damping * pr[s] / len(out_links[s])
                for t in out_links[s]:
                    nxt[t] += share
        pr = nxt
    top = max(pr.values())
    import math
    return {u: round(100 * math.log1p(v / top * 99) / math.log(100), 2) for u, v in pr.items()}  # 0–100 log scale


def link_suggestions(pages: list[CrawlPage], edges: list[tuple[str, str]], pr: dict[str, float], limit: int = 100) -> list[dict]:
    linked = defaultdict(set)
    inl = Counter()
    for s, t in edges:
        linked[s].add(t)
        inl[t] += 1
    tokens = {p.normalized_url: {t for t in match_key(f"{p.title or ''} {' '.join(p.h1 or [])}").split() if len(t) > 3}
              for p in pages if p.status_code == 200}
    targets = sorted((u for u in tokens if inl[u] <= 2 and tokens[u]), key=lambda u: (pr.get(u, 0), u))[:200]
    sources = sorted(tokens, key=lambda u: -pr.get(u, 0))[:200]
    out = []
    for t in targets:
        best = []
        for s in sources:
            if s == t or t in linked[s] or not tokens[s]:
                continue
            sim = len(tokens[s] & tokens[t]) / len(tokens[s] | tokens[t])
            if sim >= 0.2:
                best.append((round(sim * 0.6 + pr.get(s, 0) / 100 * 0.4, 3), s, sim))
        for score, s, sim in sorted(best, reverse=True)[:3]:
            page = next(p for p in pages if p.normalized_url == t)
            out.append({"source_url": s, "target_url": t, "score": score, "topic_similarity": round(sim, 2),
                        "source_pagerank": pr.get(s), "suggested_anchor": (page.h1 or [page.title or ""])[0][:80]})
    return sorted(out, key=lambda r: -r["score"])[:limit]


def crawl_trap_findings(pages: list[CrawlPage]) -> list[dict]:
    by_path: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        parts = urlsplit(p.normalized_url)
        if parts.query:
            by_path[f"{parts.scheme}://{parts.netloc}{parts.path}"].append(p.normalized_url)
    out = []
    for path, urls in sorted(by_path.items()):
        if len(urls) >= 20:
            params = Counter(k.split("=")[0] for u in urls for k in urlsplit(u).query.split("&"))
            out.append(_f("parameter_explosion", "medium", path, "Many parameterized URL variants",
                          "One path appears with many query-parameter combinations (facets, sorting, calendars or session IDs). "
                          "This can create a crawl trap and duplicate content.",
                          {"variants_crawled": len(urls), "top_params": params.most_common(5), "sample": urls[:5]},
                          "Restrict crawlable facets (robots.txt / nofollow on filter links), canonicalize variants, avoid session IDs in URLs."))
    return out


RULE_CATALOG: dict[str, tuple[str, str, str, int]] = {
    # rule_id: (category, root_cause, seo_impact, base_priority)
    "http_5xx": ("Crawlability", "Backend application error or upstream proxy failure returning HTTP 5xx.", "Critical: Search engines drop or de-index failing pages and reduce site crawl budget.", 98),
    "redirect_loop": ("Redirects", "Conflicting server/router rewrite rules redirecting URLs back onto themselves.", "Critical: Bots and users cannot reach the destination page; all link equity is lost.", 96),
    "http_4xx": ("Crawlability", "Deleted or moved resource without a 301 redirect or updated internal links.", "High: Page cannot be indexed and internal/external equity pointing to it is wasted.", 88),
    "fetch_failed": ("Crawlability", "DNS, TLS, timeout, or network failure prevented fetching the URL.", "High: Unreachable URLs cannot be crawled or indexed by search engines.", 86),
    "broken_internal_link": ("Links", "Internal anchor tags reference non-existent or failing destination URLs.", "High: Wastes crawl budget, breaks user navigation, and leaks internal PageRank.", 85),
    "title_missing": ("On-Page", "HTML <head> template lacks a non-empty <title> element.", "High: Search engines must fabricate a SERP title, hurting relevance and CTR.", 84),
    "canonical_multiple": ("Indexability", "Multiple <link rel='canonical'> tags rendered by layout or CMS plugins.", "High: Search engines ignore conflicting canonical signals and may index duplicates.", 83),
    "canonical_invalid_target": ("Indexability", "Canonical tag points to a 4xx/5xx/redirect/noindex URL.", "High: Sends contradictory consolidation signals to search engines.", 82),
    "noindex_meta": ("Indexability", "Meta robots tag includes 'noindex' directive on a crawled HTML page.", "High: Explicitly blocks the URL from appearing in organic search results.", 80),
    "noindex_header": ("Indexability", "HTTP X-Robots-Tag response header includes 'noindex'.", "High: Explicitly excludes the resource from search engine indexes.", 80),
    "hreflang_invalid_target": ("International SEO", "Hreflang alternate link points to a non-200 or redirecting URL.", "High: Search engines discard invalid hreflang clusters, breaking locale targeting.", 78),
    "hreflang_canonical_conflict": ("International SEO", "Hreflang annotation points to a non-self-canonical URL.", "High: Contradictory signals cause search engines to ignore regional alternates.", 77),
    "robots_blocked": ("Crawlability", "robots.txt Disallow rule blocks crawler access to the URL.", "Medium: Crawlers cannot inspect page content; contradictory if listed in XML sitemap.", 72),
    "canonical_missing": ("Indexability", "Page does not declare a self-referencing <link rel='canonical'>.", "Medium: Parameterized or protocol/host variants can cause duplicate indexing.", 68),
    "h1_missing": ("On-Page", "Page body does not contain a non-empty <h1> heading.", "Medium: Weakens primary topical heading hierarchy for crawlers and accessibility.", 65),
    "title_multiple": ("On-Page", "Multiple <title> tags present in HTML document.", "Medium: Ambiguous SERP title selection by search engines.", 62),
    "duplicate_title": ("On-Page", "Multiple distinct URLs share the exact same <title> text.", "Medium: Keyword cannibalization and lower SERP differentiation across pages.", 64),
    "redirect_chain": ("Redirects", "Multi-hop redirect sequence (2+ hops) before reaching final 200 URL.", "Medium: Adds latency, dampens link equity transfer, and risks crawler drop-off.", 63),
    "orphan_page": ("Links", "Page receives zero internal incoming links from the site graph.", "Medium: Poor crawl discovery frequency and near-zero internal PageRank.", 66),
    "parameter_explosion": ("Crawlability", "Unbounded URL query parameter combinations (facets/sorts) are crawlable.", "Medium: Creates crawl traps that exhaust crawl budget on duplicate views.", 67),
    "structured_data_invalid": ("Structured Data", "JSON-LD script contains malformed JSON syntax.", "Medium: Rich result eligibility is completely lost for the broken block.", 61),
    "structured_data_missing_required": ("Structured Data", "Schema.org entity omits required properties for rich results.", "Medium: Page is ineligible for enhanced SERP rich snippets.", 60),
    "hreflang_invalid_code": ("International SEO", "Hreflang attribute uses an invalid ISO 639-1 / ISO 3166-1 code.", "Medium: Search engines ignore malformed language/region targeting.", 58),
    "hreflang_duplicate_lang": ("International SEO", "Same hreflang language code declared multiple times on one page.", "Medium: Ambiguous regional targeting signals.", 56),
    "hreflang_missing_return": ("International SEO", "Alternate page does not include a reciprocal hreflang link back.", "Medium: Unconfirmed hreflang pairs are ignored by Google.", 57),
    "title_length_issue": ("On-Page", "Title tag length is outside optimal SERP display bounds (<15 or >65 chars).", "Low: Truncated or under-informative titles reduce organic CTR.", 48),
    "meta_description_missing": ("On-Page", "Page lacks a <meta name='description'> tag.", "Low: Search engines auto-generate snippets which may have lower CTR.", 45),
    "meta_description_multiple": ("On-Page", "Multiple meta description tags rendered in <head>.", "Low: Unpredictable snippet rendering in search results.", 40),
    "meta_description_length_issue": ("On-Page", "Meta description length is too short (<50) or truncated (>160 chars).", "Low: Sub-optimal SERP snippet presentation.", 42),
    "duplicate_meta_description": ("On-Page", "Identical meta description reused across multiple URLs.", "Low: Missed opportunity for page-specific SERP messaging.", 43),
    "h1_multiple": ("On-Page", "Page contains multiple <h1> elements instead of a single primary heading.", "Low: Dilutes heading hierarchy clarity.", 38),
    "thin_content": ("Content", "Indexable page contains fewer than 150 words of body copy.", "Low: May struggle to satisfy informational depth or rank for competitive queries.", 49),
    "near_duplicate_content": ("Content", "Page body SimHash is nearly identical (Hamming <= 3) to another crawled URL.", "Low: Splits ranking signals and wastes crawl budget on redundant content.", 52),
    "images_missing_alt": ("On-Page", "One or more <img> tags lack descriptive alt attributes.", "Low: Hurts accessibility and Google Images discoverability.", 44),
    "internal_link_to_redirect": ("Links", "Internal links point to a 3xx URL instead of the final destination.", "Low: Introduces unnecessary redirect hop latency during crawling and navigation.", 41),
    "sitemap_url_not_crawled": ("Crawlability", "URL listed in sitemap.xml was not reachable via internal link graph.", "Low: Indicates weak internal linking to sitemap-declared pages.", 46),
    "crawled_not_in_sitemap": ("Indexability", "Indexable 200 HTML page is omitted from XML sitemap.", "Low: Slower discovery of updated pages by search engine crawlers.", 44),
    "deep_page": ("Links", "Page requires more than 3 clicks from the homepage.", "Low: Deeply buried pages receive less frequent crawling and lower internal authority.", 47),
    "slow_response_time": ("Performance", "Server TTFB / page download exceeded 1500ms during crawl.", "Medium: Slow server response degrades Core Web Vitals (LCP/TTFB) and crawl rate.", 69),
    "non_https_url": ("Security", "Page is served over unencrypted HTTP instead of HTTPS.", "Medium: HTTPS is a confirmed Google ranking signal and browser trust requirement.", 70),
    "few_internal_inlinks": ("Links", "Indexable page at depth >= 2 has only a single internal incoming link.", "Info: Weak internal link reinforcement.", 25),
    "canonical_to_other_page": ("Indexability", "Page canonicalizes to a different URL.", "Info: Page will be consolidated into canonical target.", 20),
    "non_html": ("Crawlability", "Crawled URL returned a non-HTML MIME type.", "Info: Informational asset record.", 15),
    "structured_data_missing_recommended": ("Structured Data", "Schema.org block omits optional recommended properties.", "Info: Adding recommended fields can enrich rich snippet appearance.", 22),
    "unminified_resources": ("Performance", "HTML page loads unminified JavaScript or CSS assets with extra whitespace and comments.", "Low: Unminified resources slow down page load times (Core Web Vitals FCP/LCP) and waste bandwidth.", 45),
    "duplicate_h1_and_title": ("On-Page", "The page's primary <h1> heading is an exact duplicate of the <title> tag.", "Low: Limits keyword diversity and missed opportunity to target secondary search intent.", 42),
    "link_missing_anchor_text": ("Links", "Internal anchor link contains empty or whitespace-only text.", "Low: Search engines cannot associate topical keywords with the destination URL.", 43),
    "llms_txt_missing": ("AI Search / LLMs", "Site lacks an /llms.txt file to guide AI crawlers and LLM search systems.", "Info: Informational guidance file for modern AI crawlers and search assistants.", 20),
    "internal_link_has_nofollow": ("Links", "Internal anchor link contains rel='nofollow', preventing PageRank flow within the site.", "Low: Wastes internal link equity and causes crawl equity loss without preventing crawling.", 46),
}

DEFAULT_PRIORITY_BY_SEV = {"critical": 95, "high": 80, "medium": 60, "low": 40, "info": 20}


def enrich_finding_metadata(rule_id: str, severity: str, title: str = "", description: str = "") -> dict:
    cat, cause, impact, prio = RULE_CATALOG.get(
        rule_id,
        (
            "Technical SEO",
            description or f"Rule {rule_id} triggered during technical crawl.",
            f"{severity.capitalize()} impact on technical SEO health and crawl efficiency.",
            DEFAULT_PRIORITY_BY_SEV.get(severity, 50),
        ),
    )
    return {
        "category": cat,
        "root_cause": cause,
        "seo_impact": impact,
        "priority": prio,
    }


def image_findings(pages: list[CrawlPage]) -> list[dict]:
    return [_f("images_missing_alt", "low", p.normalized_url, "Images without alt text",
               "Images lack alternative text (accessibility; image search context).", {"count": p.images_missing_alt},
               "Add descriptive alt text to meaningful images; use empty alt for decorative ones.")
            for p in pages if (p.images_missing_alt or 0) > 0 and p.status_code == 200]


def extended_technical_findings(pages: list[CrawlPage]) -> list[dict]:
    """Additional technical SEO checks (§6): title/meta length bounds, slow response time, HTTPS check."""
    out: list[dict] = []
    for p in pages:
        if p.status_code != 200 or not p.is_html or p.fetch_error:
            continue
        if p.title and (len(p.title.strip()) > 65):
            out.append(_f(
                "title_length_issue",
                "low",
                p.normalized_url,
                "Title tag exceeds 65 characters",
                f"Title length is {len(p.title.strip())} characters and may be truncated in SERPs.",
                {"length": len(p.title.strip()), "title": p.title[:120]},
                "Keep the <title> between 30 and 60 characters with primary keywords near the front.",
            ))
        if p.meta_description and len(p.meta_description.strip()) > 160:
            out.append(_f(
                "meta_description_length_issue",
                "low",
                p.normalized_url,
                "Meta description exceeds 160 characters",
                f"Meta description length is {len(p.meta_description.strip())} characters.",
                {"length": len(p.meta_description.strip())},
                "Keep meta descriptions concise (70–155 characters) so they do not truncate in search snippets.",
            ))
        if (p.response_time_ms or 0) > 1500:
            out.append(_f(
                "slow_response_time",
                "medium",
                p.normalized_url,
                "Slow server response time (>1500ms)",
                f"Page took {p.response_time_ms}ms to respond during crawl.",
                {"response_time_ms": p.response_time_ms, "threshold_ms": 1500},
                "Optimize server TTFB, database queries, and edge caching.",
            ))
        host = (urlsplit(p.normalized_url).hostname or "").lower()
        if p.normalized_url.startswith("http://") and host not in ("127.0.0.1", "localhost") and not host.endswith(".test"):
            out.append(_f(
                "non_https_url",
                "medium",
                p.normalized_url,
                "Page served over unencrypted HTTP",
                "URL uses http:// instead of https://.",
                {"scheme": "http"},
                "Enforce HTTPS with 301 redirects and HSTS headers.",
            ))
    return out


def health_score(pages: list[CrawlPage], findings: list[AuditFinding]) -> float | None:
    html_pages = [p for p in pages if p.is_html or p.status_code]
    if not html_pages:
        return None
    pen: dict[str, float] = defaultdict(float)
    for f in findings:
        if f.url:
            pen[f.url] += SEVERITY_WEIGHT.get(f.severity, 0)
    return round(100 * (1 - sum(min(1.0, pen[p.normalized_url] / 10) for p in html_pages) / len(html_pages)), 1)


def template_issues(db: Session, crawl_id: int) -> list[dict]:
    rows = db.execute(select(CrawlPage.template_sig, AuditFinding.rule_id, AuditFinding.severity)
                      .join(CrawlPage, CrawlPage.id == AuditFinding.page_id)
                      .where(AuditFinding.crawl_run_id == crawl_id, CrawlPage.template_sig.is_not(None))).all()
    sizes = Counter(db.scalars(select(CrawlPage.template_sig).where(CrawlPage.crawl_run_id == crawl_id,
                                                                    CrawlPage.template_sig.is_not(None))))
    agg = Counter((t, r, s) for t, r, s in rows)
    out = [{"template": t, "rule_id": r, "severity": s, "affected_pages": n, "template_pages": sizes[t],
            "share": round(n / sizes[t], 2)} for (t, r, s), n in agg.items() if sizes[t] >= 3 and n / sizes[t] >= 0.8]
    return sorted(out, key=lambda x: (-SEVERITY_WEIGHT.get(x["severity"], 0), -x["affected_pages"]))


# ---------------------------------------------------------------- orchestration

def run(db: Session, crawl_id: int) -> dict:
    """Append advanced findings to the crawl (idempotent via fingerprints), store PageRank and health score."""
    from app.services.crawl_service import finding_fingerprint
    run_ = db.get(CrawlRun, crawl_id)
    pages = db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == crawl_id)).all()
    by_id = {p.id: p.normalized_url for p in pages}
    edges = [(by_id[s], t) for s, t in db.execute(
        select(PageLink.source_page_id, PageLink.target_url).join(CrawlPage, CrawlPage.id == PageLink.source_page_id)
        .where(CrawlPage.crawl_run_id == crawl_id, PageLink.is_internal.is_(True)))]
    pr = internal_pagerank(pages, edges)
    for p in pages:
        p.internal_pagerank = pr.get(p.normalized_url)
    new = (
        structured_data_findings(pages)
        + near_duplicates(pages)
        + crawl_trap_findings(pages)
        + image_findings(pages)
        + extended_technical_findings(pages)
    )
    page_ids = {p.normalized_url: p.id for p in pages}
    existing = set(db.scalars(select(AuditFinding.fingerprint).where(AuditFinding.crawl_run_id == crawl_id)))
    for f in new:
        fp = finding_fingerprint(f["rule_id"], f["url"], f["evidence"])
        if fp in existing:
            continue
        existing.add(fp)
        meta = enrich_finding_metadata(f["rule_id"], f["severity"], f["title"], f["description"])
        db.add(AuditFinding(
            crawl_run_id=crawl_id,
            page_id=page_ids.get(f["url"]),
            url=f["url"],
            rule_id=f["rule_id"],
            severity=f["severity"],
            title=f["title"],
            description=f["description"],
            evidence=json.loads(json.dumps(f["evidence"], default=str)),
            remediation=f["remediation"],
            fingerprint=fp,
            status="open",
            category=meta["category"],
            root_cause=meta["root_cause"],
            seo_impact=meta["seo_impact"],
            priority=meta["priority"],
        ))
    db.flush()
    findings = db.scalars(select(AuditFinding).where(AuditFinding.crawl_run_id == crawl_id)).all()
    for fn in findings:
        if not fn.category or not fn.root_cause:
            meta = enrich_finding_metadata(fn.rule_id, fn.severity, fn.title, fn.description)
            fn.category = fn.category or meta["category"]
            fn.root_cause = fn.root_cause or meta["root_cause"]
            fn.seo_impact = fn.seo_impact or meta["seo_impact"]
            if not fn.priority:
                fn.priority = meta["priority"]
    run_.health_score = health_score(pages, findings)
    db.commit()
    return {"advanced_findings": len(new), "health_score": run_.health_score}


def audit_report(db: Session, crawl_id: int) -> dict:
    """Comprehensive Modül 4 Site Audit & Performance Report (§6)."""
    from app.core.config import get_settings
    from app.services.domains import make_provenance

    run_ = db.get(CrawlRun, crawl_id)
    pages = db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == crawl_id)).all()
    findings = db.scalars(select(AuditFinding).where(AuditFinding.crawl_run_id == crawl_id).order_by(AuditFinding.priority.desc(), AuditFinding.id)).all()

    by_severity = {s: 0 for s in ("critical", "high", "medium", "low", "info")}
    by_status = {s: 0 for s in ("open", "in_progress", "fixed", "ignored")}
    by_category: dict[str, int] = defaultdict(int)
    grouped: dict[str, dict] = {}

    for f in findings:
        meta = enrich_finding_metadata(f.rule_id, f.severity, f.title, f.description)
        cat = f.category or meta["category"]
        st = f.status or "open"
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_status[st] = by_status.get(st, 0) + 1
        by_category[cat] += 1

        g = grouped.setdefault(f.rule_id, {
            "rule_id": f.rule_id,
            "title": f.title,
            "category": cat,
            "severity": f.severity,
            "priority": f.priority or meta["priority"],
            "description": f.description,
            "root_cause": f.root_cause or meta["root_cause"],
            "seo_impact": f.seo_impact or meta["seo_impact"],
            "remediation": f.remediation,
            "affected_count": 0,
            "affected_urls": [],
            "status_counts": {"open": 0, "in_progress": 0, "fixed": 0, "ignored": 0},
        })
        g["affected_count"] += 1
        if f.url and len(g["affected_urls"]) < 25:
            g["affected_urls"].append(f.url)
        g["status_counts"][st] = g["status_counts"].get(st, 0) + 1

    rt_vals = [p.response_time_ms for p in pages if p.response_time_ms is not None]
    avg_rt = round(sum(rt_vals) / len(rt_vals), 1) if rt_vals else None
    slow_pages = [p.normalized_url for p in pages if (p.response_time_ms or 0) > 1500]
    psi_configured = bool(get_settings().crux_api_key)

    return {
        "crawl_id": crawl_id,
        "site_id": run_.site_id,
        "status": run_.status,
        "health_score": run_.health_score if run_.health_score is not None else health_score(pages, findings),
        "total_pages": len(pages),
        "total_findings": len(findings),
        "by_severity": by_severity,
        "by_status": by_status,
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
        "grouped_issues": sorted(grouped.values(), key=lambda x: (-x["priority"], -x["affected_count"])),
        "performance_summary": {
            "avg_response_time_ms": avg_rt,
            "slow_pages_count": len(slow_pages),
            "slow_pages_sample": slow_pages[:20],
            "pagespeed_api_status": "CONNECTED" if psi_configured else "INTEGRATION_CREDENTIALS_MISSING",
            "pagespeed_required_env": None if psi_configured else "CRUX_API_KEY",
        },
        "provenance": make_provenance(
            "Internal_Crawler",
            "OBSERVED",
            run_.started_at.isoformat() if run_.started_at else None,
            run_.finished_at.isoformat() if run_.finished_at else None,
            False,
            0.95,
        ),
    }


def previous_crawl(db: Session, run_: CrawlRun) -> CrawlRun | None:
    return db.scalar(select(CrawlRun).where(CrawlRun.site_id == run_.site_id, CrawlRun.competitor_id.is_(None),
                                            CrawlRun.status == "completed", CrawlRun.id < run_.id).order_by(CrawlRun.id.desc()).limit(1))


def crawl_diff(db: Session, crawl_id: int) -> dict:
    cur = db.get(CrawlRun, crawl_id)
    prev = previous_crawl(db, cur)
    if prev is None:
        return {"available": False, "reason": "no previous completed crawl of this site"}
    site = db.get(Site, cur.site_id)
    money = [re.compile(p) for p in (site.money_page_patterns or [])[:20]]

    def load(r):
        return {p.normalized_url: p for p in db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == r.id))}
    a, b = load(prev), load(cur)
    alerts = []

    def alert(kind, sev, url, details):
        alerts.append({"type": kind, "severity": sev, "url": url, "details": details})
    site_host = urlsplit(site.base_url).hostname
    for u in sorted(set(a) & set(b)):
        pa, pb = a[u], b[u]
        is_money = any(m.search(urlsplit(u).path) for m in money)
        if is_money and "noindex" in f"{pb.meta_robots or ''} {pb.x_robots_tag or ''}".lower() and \
                "noindex" not in f"{pa.meta_robots or ''} {pa.x_robots_tag or ''}".lower():
            alert("money_page_noindex", "critical", u, {"meta_robots": pb.meta_robots, "x_robots_tag": pb.x_robots_tag})
        if (pb.status_code or 0) >= 500 and (pa.status_code or 0) < 500:
            alert("became_5xx", "critical" if is_money else "high", u, {"from": pa.status_code, "to": pb.status_code})
        if pb.canonical_url and urlsplit(pb.canonical_url).hostname not in (None, site_host) and pb.canonical_url != pa.canonical_url:
            alert("canonical_other_host", "high", u, {"from": pa.canonical_url, "to": pb.canonical_url})
    if re.search(r"(?im)^\s*disallow:\s*/\s*$", cur.robots_txt or "") and not re.search(r"(?im)^\s*disallow:\s*/\s*$", prev.robots_txt or ""):
        alert("robots_disallow_all", "critical", site.base_url, {"robots_txt_excerpt": (cur.robots_txt or "")[:500]})
    dropped = sorted(set(prev.sitemap_urls or []) - set(cur.sitemap_urls or []))
    if dropped:
        alert("sitemap_urls_dropped", "medium" if len(dropped) < 50 else "high", None, {"count": len(dropped), "sample": dropped[:20]})
    s5a = sum(1 for p in a.values() if (p.status_code or 0) >= 500)
    s5b = sum(1 for p in b.values() if (p.status_code or 0) >= 500)
    if s5b > max(2, s5a * 2):
        alert("5xx_increase", "high", None, {"previous": s5a, "current": s5b})
    return {"available": True, "previous_crawl": prev.id, "current_crawl": cur.id,
            "urls": {"added": sorted(set(b) - set(a))[:500], "removed": sorted(set(a) - set(b))[:500]},
            "status_changes": [{"url": u, "from": a[u].status_code, "to": b[u].status_code}
                               for u in sorted(set(a) & set(b)) if a[u].status_code != b[u].status_code][:500],
            "health_score": {"previous": prev.health_score, "current": cur.health_score},
            "regression_alerts": alerts}

