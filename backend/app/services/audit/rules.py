"""Individual audit rules. Each rule: (AuditInput) -> Iterable[Finding], tagged via @rule."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from app.services.audit.types import AuditInput, Finding, PageInput

RuleFn = Callable[[AuditInput], Iterable[Finding]]
RULES: list[RuleFn] = []

SOURCE_CAP = 20
THIN_WORDS = 150
MAX_DEPTH = 3
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?$", re.I)
_NOINDEX_RE = re.compile(r"(?<![\w-])(noindex|none)(?![\w-])", re.I)


def rule(rule_id: str, severity: str) -> Callable[[RuleFn], RuleFn]:
    """Register a rule and attach its metadata (rule_id, default severity)."""

    def deco(fn: RuleFn) -> RuleFn:
        fn.rule_id = rule_id  # type: ignore[attr-defined]
        fn.severity = severity  # type: ignore[attr-defined]
        RULES.append(fn)
        return fn

    return deco


# ---------- helpers ----------

def _f(rid: str, sev: str, url: str | None, title: str, desc: str, rem: str,
       **evidence: Any) -> Finding:
    return Finding(rid, sev, url, title, desc, evidence, rem)


def _redirected(p: PageInput) -> bool:
    return bool(p.redirect_chain) or (p.status_code is not None and 300 <= p.status_code < 400)


def _is_loop(p: PageInput) -> bool:
    urls = [u for u, _ in p.redirect_chain]
    if len(urls) != len(set(urls)):
        return True
    err = (p.fetch_error or "").lower()
    return "loop" in err or "too many redirects" in err


def _ok_html(p: PageInput) -> bool:
    """200 HTML page, not redirected, not failed: eligible for content rules."""
    return (p.status_code == 200 and p.is_html and not p.fetch_error
            and not p.redirect_chain)


def _noindex_meta(p: PageInput) -> bool:
    return bool(p.meta_robots and _NOINDEX_RE.search(p.meta_robots))


def _noindex_header(p: PageInput) -> bool:
    return bool(p.x_robots_tag and _NOINDEX_RE.search(p.x_robots_tag))


def _indexable(p: PageInput) -> bool:
    return _ok_html(p) and not _noindex_meta(p) and not _noindex_header(p)


def _failed(p: PageInput) -> bool:
    return bool(p.fetch_error) or p.status_code is None or p.status_code >= 400


def _by_url(inp: AuditInput) -> dict[str, PageInput]:
    return {p.url: p for p in inp.pages}


def _inlinks(inp: AuditInput) -> dict[str, set[str]]:
    """target -> distinct internal source urls (self-links excluded)."""
    out: dict[str, set[str]] = defaultdict(set)
    for link in inp.links:
        if link.is_internal and link.source_url != link.target_url:
            out[link.target_url].add(link.source_url)
    return out


def _clean(values: list[str]) -> list[str]:
    return [v for v in values if v and v.strip()]


def _norm_text(s: str) -> str:
    return " ".join(s.split()).casefold()


def _canon_other(p: PageInput) -> str | None:
    """The single canonical target if it differs from the page's own URL."""
    cs = set(_clean(p.canonicals))
    if len(cs) != 1:
        return None
    c = next(iter(cs)).split("#")[0]
    return c if c != p.url else None


def _bad_target_reason(t: PageInput | None) -> str | None:
    if t is None:
        return None
    if _redirected(t):
        return "redirect"
    if _failed(t):
        return f"status_{t.status_code}" if t.status_code else "fetch_failed"
    if t.status_code != 200:
        return f"status_{t.status_code}"
    if _noindex_meta(t) or _noindex_header(t):
        return "noindex"
    return None


# ---------- HTTP / fetch ----------

@rule("http_4xx", "high")
def http_4xx(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if p.status_code is not None and 400 <= p.status_code < 500 and not p.redirect_chain:
            yield _f("http_4xx", "high", p.url, f"HTTP {p.status_code} client error",
                     f"The URL returns {p.status_code}. Indexing issue: it cannot be indexed "
                     "and any links pointing to it lead to a dead end.",
                     "Restore the page, redirect it to a relevant URL, or remove links to it.",
                     status_code=p.status_code)


@rule("http_5xx", "critical")
def http_5xx(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if p.status_code is not None and p.status_code >= 500 and not p.redirect_chain:
            yield _f("http_5xx", "critical", p.url, f"HTTP {p.status_code} server error",
                     f"The URL returns {p.status_code}. Indexing issue: crawlers cannot fetch "
                     "it and repeated errors can reduce crawling of the site.",
                     "Fix the server-side error and monitor availability.",
                     status_code=p.status_code)


@rule("fetch_failed", "high")
def fetch_failed(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if p.fetch_error and not _is_loop(p):
            yield _f("fetch_failed", "high", p.url, "Page could not be fetched",
                     "The crawler failed to fetch this URL (network/timeout/protocol error). "
                     "Indexing issue: if crawlers fail the same way the page cannot be indexed.",
                     "Check DNS, TLS, timeouts and server availability for this URL.",
                     error=p.fetch_error)


@rule("non_html", "info")
def non_html(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if p.status_code == 200 and not p.is_html and not p.fetch_error and not p.redirect_chain:
            yield _f("non_html", "info", p.url, "Non-HTML resource",
                     "This URL serves a non-HTML resource, so HTML content rules were skipped. "
                     "Informational only.",
                     "No action needed unless this URL was meant to be an HTML page.",
                     content_type=p.content_type)


# ---------- titles / meta / h1 ----------

@rule("title_missing", "high")
def title_missing(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and not _clean(p.titles):
            yield _f("title_missing", "high", p.url, "Missing <title>",
                     "The page has no non-empty <title>. Quality hint with indexing impact: "
                     "search engines may generate their own title for results.",
                     "Add a unique, descriptive <title> to the page.")


@rule("title_multiple", "medium")
def title_multiple(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and len(_clean(p.titles)) > 1:
            yield _f("title_multiple", "medium", p.url, "Multiple <title> elements",
                     "The page declares more than one <title>. Quality hint: it is ambiguous "
                     "which title search engines will use.",
                     "Keep a single <title> element.", titles=_clean(p.titles))


@rule("meta_description_missing", "low")
def meta_description_missing(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and not _clean(p.meta_descriptions):
            yield _f("meta_description_missing", "low", p.url, "Missing meta description",
                     "No meta description. Quality hint only: meta descriptions are not a "
                     "ranking factor, but can influence the snippet shown in results.",
                     "Add a concise, page-specific meta description if the snippet matters.")


@rule("meta_description_multiple", "low")
def meta_description_multiple(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and len(_clean(p.meta_descriptions)) > 1:
            yield _f("meta_description_multiple", "low", p.url, "Multiple meta descriptions",
                     "More than one meta description. Quality hint: search engines pick one, "
                     "so the snippet may not be what you intend. Not a ranking factor.",
                     "Keep a single meta description.",
                     meta_descriptions=_clean(p.meta_descriptions))


@rule("h1_missing", "medium")
def h1_missing(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and not _clean(p.h1):
            yield _f("h1_missing", "medium", p.url, "Missing H1",
                     "The page has no non-empty H1. Quality hint: a clear main heading helps "
                     "users and crawlers understand the page topic.",
                     "Add one descriptive H1 to the page.")


@rule("h1_multiple", "low")
def h1_multiple(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and len(_clean(p.h1)) > 1:
            yield _f("h1_multiple", "low", p.url, "Multiple H1 headings",
                     "The page has several H1s. Quality hint only: HTML5 permits it, but a "
                     "single main heading is clearer.",
                     "Consider using a single H1 and H2s for sub-sections.",
                     h1=_clean(p.h1))


# ---------- canonical ----------

@rule("canonical_missing", "medium")
def canonical_missing(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and not _clean(p.canonicals):
            yield _f("canonical_missing", "medium", p.url, "Missing canonical",
                     "No rel=canonical. Indexing issue: duplicate URL variants may be "
                     "consolidated on a URL other than the one you prefer.",
                     "Add a self-referencing absolute canonical link.")


@rule("canonical_multiple", "high")
def canonical_multiple(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        cs = sorted(set(_clean(p.canonicals)))
        if _ok_html(p) and len(cs) > 1:
            yield _f("canonical_multiple", "high", p.url, "Conflicting canonicals",
                     "Multiple different canonical URLs. Indexing issue: search engines "
                     "ignore conflicting canonicals and choose on their own.",
                     "Declare exactly one canonical URL.", canonicals=cs)


@rule("canonical_invalid_target", "high")
def canonical_invalid_target(inp: AuditInput) -> Iterable[Finding]:
    pages = _by_url(inp)
    for p in inp.pages:
        if not _ok_html(p):
            continue
        c = _canon_other(p)
        reason = _bad_target_reason(pages.get(c)) if c else None
        if c and reason:
            yield _f("canonical_invalid_target", "high", p.url, "Canonical points to unusable URL",
                     f"The canonical target was crawled and is a problem ({reason}). Indexing "
                     "issue: canonical signals to such targets are ignored or harmful.",
                     "Point the canonical at an indexable 200 URL.",
                     canonical=c, reason=reason)


@rule("canonical_to_other_page", "info")
def canonical_to_other_page(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        c = _canon_other(p)
        if _ok_html(p) and c:
            yield _f("canonical_to_other_page", "info", p.url, "Canonicalised to another URL",
                     "This page declares a different canonical URL, so it is expected to be "
                     "consolidated into it and not indexed itself. Informational.",
                     "Verify this is intended.", canonical=c)


# ---------- robots / noindex ----------

def _noindex_sev(inp: AuditInput, p: PageInput) -> str:
    return "high" if p.url in inp.sitemap_urls or p.url in _inlinks(inp) else "medium"


@rule("noindex_meta", "high")
def noindex_meta(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if p.status_code == 200 and p.is_html and _noindex_meta(p):
            yield _f("noindex_meta", _noindex_sev(inp, p), p.url, "Page has meta noindex",
                     "Meta robots contains noindex. Indexing issue: the page is excluded from "
                     "search results; severe if it is in the sitemap or linked internally.",
                     "Remove noindex if the page should be indexed.",
                     meta_robots=p.meta_robots, in_sitemap=p.url in inp.sitemap_urls)


@rule("noindex_header", "high")
def noindex_header(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if p.status_code == 200 and _noindex_header(p):
            yield _f("noindex_header", _noindex_sev(inp, p), p.url, "Page has X-Robots-Tag noindex",
                     "The X-Robots-Tag header contains noindex. Indexing issue: the URL is "
                     "excluded from search results; severe if in sitemap or linked internally.",
                     "Remove noindex from the header if the page should be indexed.",
                     x_robots_tag=p.x_robots_tag, in_sitemap=p.url in inp.sitemap_urls)


@rule("robots_blocked", "info")
def robots_blocked(inp: AuditInput) -> Iterable[Finding]:
    for u in sorted(inp.robots_blocked_urls):
        in_sm = u in inp.sitemap_urls
        yield _f("robots_blocked", "high" if in_sm else "info", u, "URL blocked by robots.txt",
                 "robots.txt disallows crawling this URL, so it was not audited. Indexing "
                 "issue only if the page should be found"
                 + ("; it is listed in the sitemap, which is contradictory." if in_sm else "."),
                 "Allow the URL in robots.txt or drop it from the sitemap.", in_sitemap=in_sm)


# ---------- hreflang ----------

@rule("hreflang_invalid_code", "medium")
def hreflang_invalid_code(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if not _ok_html(p):
            continue
        bad = sorted({lang for lang, _ in p.hreflang
                      if lang.lower() != "x-default" and not _LANG_RE.match(lang)})
        if bad:
            yield _f("hreflang_invalid_code", "medium", p.url, "Invalid hreflang code",
                     "hreflang values are not valid language[-script][-region] codes. "
                     "Indexing issue: invalid annotations are ignored.",
                     "Use ISO 639-1 language and optional ISO 3166-1 region, or x-default.",
                     invalid_codes=bad)


@rule("hreflang_duplicate_lang", "medium")
def hreflang_duplicate_lang(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if not _ok_html(p):
            continue
        seen: dict[str, set[str]] = defaultdict(set)
        count: dict[str, int] = defaultdict(int)
        for lang, href in p.hreflang:
            seen[lang.lower()].add(href)
            count[lang.lower()] += 1
        dups = sorted(k for k, n in count.items() if n > 1)
        if dups:
            yield _f("hreflang_duplicate_lang", "medium", p.url, "Duplicate hreflang language",
                     "The same hreflang code is declared more than once. Indexing issue: "
                     "ambiguous alternates may be ignored.",
                     "Declare each hreflang code once.", duplicate_langs=dups)


@rule("hreflang_invalid_target", "high")
def hreflang_invalid_target(inp: AuditInput) -> Iterable[Finding]:
    pages = _by_url(inp)
    for p in inp.pages:
        if not _ok_html(p):
            continue
        for lang, href in sorted(set(p.hreflang)):
            t = pages.get(href)
            if t is not None and (
                _redirected(t) or _failed(t) or t.status_code != 200
            ):
                yield _f("hreflang_invalid_target", "high", p.url, "hreflang target not 200",
                         "An hreflang alternate is a redirect or error page. Indexing issue: "
                         "search engines ignore such alternates.",
                         "Point hreflang at the final 200 URL.",
                         lang=lang, target=href, target_status=t.status_code)


@rule("hreflang_missing_return", "medium")
def hreflang_missing_return(inp: AuditInput) -> Iterable[Finding]:
    pages = _by_url(inp)
    for p in inp.pages:
        if not _ok_html(p):
            continue
        for lang, href in sorted(set(p.hreflang)):
            t = pages.get(href)
            if t is None or href == p.url or not _ok_html(t):
                continue
            if p.url not in {h for _, h in t.hreflang}:
                yield _f("hreflang_missing_return", "medium", p.url, "hreflang missing return link",
                         "The alternate page does not link back. Indexing issue: hreflang "
                         "requires reciprocal annotations or they are ignored.",
                         "Add the reciprocal hreflang on the target page.",
                         lang=lang, target=href)


@rule("hreflang_canonical_conflict", "high")
def hreflang_canonical_conflict(inp: AuditInput) -> Iterable[Finding]:
    pages = _by_url(inp)
    for p in inp.pages:
        if not _ok_html(p):
            continue
        for lang, href in sorted(set(p.hreflang)):
            t = pages.get(href)
            c = _canon_other(t) if t is not None and _ok_html(t) else None
            if c:
                yield _f("hreflang_canonical_conflict", "high", p.url,
                         "hreflang target canonicalises elsewhere",
                         "The hreflang alternate declares a different canonical. Indexing "
                         "issue: hreflang and canonical signals conflict, so the alternate "
                         "is likely ignored.",
                         "Make hreflang targets self-canonical.",
                         lang=lang, target=href, target_canonical=c)


# ---------- links / redirects ----------

@rule("broken_internal_link", "high")
def broken_internal_link(inp: AuditInput) -> Iterable[Finding]:
    pages = _by_url(inp)
    sources: dict[str, set[str]] = defaultdict(set)
    for link in inp.links:
        t = pages.get(link.target_url)
        if link.is_internal and t is not None and not _redirected(t) and _failed(t):
            sources[link.target_url].add(link.source_url)
    for target in sorted(sources):
        srcs = sorted(sources[target])
        t = pages[target]
        yield _f("broken_internal_link", "high", target, "Broken internal link target",
                 "Internal links point to a URL that returns an error or failed to load. "
                 "Indexing/crawl-quality issue: wastes crawl budget and breaks user paths.",
                 "Fix the links or restore/redirect the target.",
                 status_code=t.status_code, fetch_error=t.fetch_error,
                 source_count=len(srcs), sources=srcs[:SOURCE_CAP])


@rule("redirect_chain", "medium")
def redirect_chain(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if len(p.redirect_chain) >= 2 and not _is_loop(p):
            yield _f("redirect_chain", "medium", p.url, "Redirect chain",
                     f"The URL needs {len(p.redirect_chain)} redirect hops. Indexing/crawl "
                     "issue: chains slow crawling and may be abandoned by crawlers.",
                     "Redirect directly to the final URL in one hop.",
                     hops=[[u, s] for u, s in p.redirect_chain], final_url=p.final_url)


@rule("redirect_loop", "critical")
def redirect_loop(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _is_loop(p):
            yield _f("redirect_loop", "critical", p.url, "Redirect loop",
                     "The URL redirects in a loop. Indexing issue: it can never be fetched "
                     "or indexed.",
                     "Fix the redirect rules so they terminate at a 200 URL.",
                     hops=[[u, s] for u, s in p.redirect_chain], error=p.fetch_error)


@rule("internal_link_to_redirect", "low")
def internal_link_to_redirect(inp: AuditInput) -> Iterable[Finding]:
    pages = _by_url(inp)
    sources: dict[str, set[str]] = defaultdict(set)
    for link in inp.links:
        t = pages.get(link.target_url)
        if (link.is_internal and t is not None
                and (_redirected(t)) and not _is_loop(t)):
            sources[link.target_url].add(link.source_url)
    for target in sorted(sources):
        srcs = sorted(sources[target])
        yield _f("internal_link_to_redirect", "low", target, "Internal links to a redirect",
                 "Internal links point to a redirecting URL. Quality hint: extra hop for "
                 "crawlers and users; not an indexing blocker.",
                 "Update links to the final destination URL.",
                 final_url=pages[target].final_url, source_count=len(srcs),
                 sources=srcs[:SOURCE_CAP])


# ---------- structure / sitemap ----------

def _start_url(inp: AuditInput, p: PageInput) -> bool:
    return p.url == inp.base_url or p.depth == 0


@rule("orphan_page", "medium")
def orphan_page(inp: AuditInput) -> Iterable[Finding]:
    inl = _inlinks(inp)
    for p in inp.pages:
        if _ok_html(p) and not _start_url(inp, p) and not inl.get(p.url):
            in_sm = p.url in inp.sitemap_urls
            yield _f("orphan_page", "medium" if in_sm else "info", p.url, "Orphan page",
                     "No internal links point to this page. Indexing/discovery issue: "
                     "crawlers rely on the sitemap alone and the page gets no internal "
                     "link signals.",
                     "Link to the page from relevant pages or remove it if obsolete.",
                     in_sitemap=in_sm)


@rule("sitemap_url_not_crawled", "low")
def sitemap_url_not_crawled(inp: AuditInput) -> Iterable[Finding]:
    if inp.crawl_truncated:
        return
    known = {p.url for p in inp.pages} | inp.robots_blocked_urls
    for u in sorted(inp.sitemap_urls - known):
        yield _f("sitemap_url_not_crawled", "low", u, "Sitemap URL not reached by crawl",
                 "A sitemap URL was never reached by the crawl. Discovery hint: it may be "
                 "orphaned, off-scope or outside the crawl depth.",
                 "Check the URL is valid and linked internally.")


@rule("crawled_not_in_sitemap", "low")
def crawled_not_in_sitemap(inp: AuditInput) -> Iterable[Finding]:
    if not inp.sitemap_found:
        return
    for p in inp.pages:
        if _indexable(p) and p.url not in inp.sitemap_urls:
            yield _f("crawled_not_in_sitemap", "low", p.url, "Indexable page missing from sitemap",
                     "Indexable page not listed in the sitemap. Discovery hint only; "
                     "sitemaps are not required for indexing.",
                     "Add the page to the sitemap if it should be indexed.")


@rule("thin_content", "low")
def thin_content(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _indexable(p) and p.word_count is not None and p.word_count < THIN_WORDS:
            yield _f("thin_content", "low", p.url, "Thin content (heuristic)",
                     f"Only {p.word_count} words. Quality hint based on a word-count heuristic, "
                     "not an indexing rule; some pages are legitimately short.",
                     "Review whether the page fully answers its search intent.",
                     word_count=p.word_count, threshold=THIN_WORDS)


def _dup_groups(inp: AuditInput, attr: str) -> Iterable[tuple[str, list[PageInput]]]:
    groups: dict[str, list[PageInput]] = defaultdict(list)
    for p in inp.pages:
        vals = _clean(getattr(p, attr))
        if _ok_html(p) and vals:
            groups[_norm_text(vals[0])].append(p)
    for key in sorted(groups):
        if len(groups[key]) > 1:
            yield key, groups[key]


@rule("duplicate_title", "medium")
def duplicate_title(inp: AuditInput) -> Iterable[Finding]:
    for key, grp in _dup_groups(inp, "titles"):
        for p in grp:
            others = sorted(o.url for o in grp if o.url != p.url)
            yield _f("duplicate_title", "medium", p.url, "Duplicate title",
                     "Other pages share this exact title. Quality hint: identical titles "
                     "make pages hard to tell apart in results; not an indexing blocker.",
                     "Give each page a unique, descriptive title.",
                     duplicate_title=key, other_count=len(others),
                     other_urls=others[:SOURCE_CAP])


@rule("duplicate_meta_description", "low")
def duplicate_meta_description(inp: AuditInput) -> Iterable[Finding]:
    for key, grp in _dup_groups(inp, "meta_descriptions"):
        for p in grp:
            others = sorted(o.url for o in grp if o.url != p.url)
            yield _f("duplicate_meta_description", "low", p.url, "Duplicate meta description",
                     "Other pages share this meta description. Quality hint only; meta "
                     "descriptions are not a ranking factor.",
                     "Write page-specific descriptions or omit them.",
                     meta_description=key, other_count=len(others),
                     other_urls=others[:SOURCE_CAP])


@rule("deep_page", "low")
def deep_page(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and p.depth is not None and p.depth > MAX_DEPTH:
            yield _f("deep_page", "low", p.url, "Page is deep in the site",
                     f"Click depth {p.depth} from the start page. Discovery/quality hint: "
                     "deep pages are crawled less often.",
                     "Link to important pages from shallower pages.", depth=p.depth)


@rule("few_internal_inlinks", "info")
def few_internal_inlinks(inp: AuditInput) -> Iterable[Finding]:
    inl = _inlinks(inp)
    for p in inp.pages:
        if _indexable(p) and p.depth is not None and p.depth >= 2 and len(inl.get(p.url, ())) == 1:
            yield _f("few_internal_inlinks", "info", p.url, "Only one internal link",
                     "Only one page links here. Quality hint about internal linking, "
                     "not an indexing issue.",
                     "Add contextual internal links if the page is important.",
                     source=sorted(inl[p.url])[0], depth=p.depth)


@rule("unminified_resources", "low")
def unminified_resources(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        if _ok_html(p) and p.unminified_resources:
            yield _f(
                "unminified_resources",
                "low",
                p.url,
                "Unminified JavaScript and CSS files",
                f"Page references {len(p.unminified_resources)} unminified JavaScript or CSS resource(s). "
                "Unminified files slow down page load time and increase bandwidth usage.",
                "Minify JavaScript and CSS files by removing whitespace, comments, and unused code.",
                unminified_count=len(p.unminified_resources),
                resources=p.unminified_resources[:SOURCE_CAP],
            )


@rule("duplicate_h1_and_title", "low")
def duplicate_h1_and_title(inp: AuditInput) -> Iterable[Finding]:
    for p in inp.pages:
        h1s = _clean(p.h1)
        titles = _clean(p.titles)
        if _ok_html(p) and h1s and titles:
            if _norm_text(h1s[0]) == _norm_text(titles[0]):
                yield _f(
                    "duplicate_h1_and_title",
                    "low",
                    p.url,
                    "H1 tag and title tag have identical text",
                    "The first H1 tag contains the exact same text as the <title> tag. "
                    "Creating distinct title and H1 tags allows targeting more keyword variations and improves snippet relevance.",
                    "Make the title tag and H1 tag complementary rather than identical.",
                    h1=h1s[0],
                    page_title=titles[0],
                )


@rule("link_missing_anchor_text", "low")
def link_missing_anchor_text(inp: AuditInput) -> Iterable[Finding]:
    by_source: dict[str, list[str]] = defaultdict(list)
    for link in inp.links:
        if link.is_internal and link.anchor_text is not None and not link.anchor_text.strip():
            by_source[link.source_url].append(link.target_url)
    for url, targets in sorted(by_source.items()):
        yield _f(
            "link_missing_anchor_text",
            "low",
            url,
            "Internal links have no anchor text",
            f"Page has {len(targets)} internal link(s) without anchor text. "
            "Descriptive anchor text helps users and search engines understand the linked page's content.",
            "Add descriptive anchor text or accessible labels (aria-label/sr-only) to all links.",
            missing_count=len(targets),
            sample_targets=targets[:SOURCE_CAP],
        )


@rule("llms_txt_missing", "info")
def llms_txt_missing(inp: AuditInput) -> Iterable[Finding]:
    if not inp.llms_txt_found:
        yield _f(
            "llms_txt_missing",
            "info",
            inp.base_url,
            "Missing /llms.txt specification",
            "The site does not provide an /llms.txt file to guide AI crawlers and LLM search engines.",
            "Add an /llms.txt file at the site root outlining key documentation and site structure for AI agents.",
        )


@rule("internal_link_has_nofollow", "low")
def internal_link_has_nofollow(inp: AuditInput) -> Iterable[Finding]:
    sources: dict[str, list[str]] = defaultdict(list)
    for link in inp.links:
        if link.is_internal and link.rel and "nofollow" in link.rel.lower().split():
            sources[link.source_url].append(link.target_url)
    for src in sorted(sources):
        targets = sorted(sources[src])
        yield _f(
            "internal_link_has_nofollow",
            "low",
            src,
            "Outgoing internal links contain nofollow attribute",
            f"Page contains {len(targets)} internal link(s) with rel='nofollow'. "
            "Search engines do not pass link equity or PageRank through internal nofollow links, causing crawl equity to be lost.",
            "Remove rel='nofollow' from internal links. Use robots noindex meta tag on the target page instead if you want to exclude it from indexing.",
            nofollow_count=len(targets),
            sample_targets=targets[:SOURCE_CAP],
        )
