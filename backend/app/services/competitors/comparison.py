"""Descriptive technical, URL, and page-type comparisons across crawl samples."""
from __future__ import annotations

from collections import Counter
from statistics import median
from urllib.parse import urlsplit

from app.services.competitors.page_types import classify_page
from app.services.competitors.types import PageSnapshot, SiteSnapshot

_PERCENT_METRICS = (
    "pct_missing_title",
    "pct_missing_meta_description",
    "pct_missing_h1",
    "pct_multiple_h1",
    "pct_missing_canonical",
    "pct_with_hreflang",
    "pct_non_200",
)
_IGNORED_MISSING_TYPES = frozenset({"login_account", "other", "legal"})
_LANG_PREFIX = frozenset({
    "ar", "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "he",
    "hi", "hr", "hu", "id", "it", "ja", "ko", "lt", "lv", "ms", "nb", "nl",
    "pl", "pt", "ro", "ru", "sk", "sl", "sr", "sv", "th", "tr", "uk", "vi",
    "zh",
})


def _html_200(pages: list[PageSnapshot]) -> list[PageSnapshot]:
    return [page for page in pages if page.status_code == 200 and page.is_html]


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def _technical(site: SiteSnapshot) -> dict:
    html_pages = _html_200(site.pages)
    total = len(html_pages)
    word_counts = [page.word_count for page in html_pages if page.word_count is not None]
    metrics: dict = {
        "label": site.label,
        "pages_crawled": len(site.pages),
        "html_200_pages": total,
        "pct_non_200": _pct(
            sum(page.status_code != 200 for page in site.pages), len(site.pages)
        ),
        "sitemap_found": site.sitemap_found,
        "sitemap_url_count": site.sitemap_url_count,
        "findings_by_rule": dict(sorted(site.findings_by_rule.items())),
    }
    if not total:
        metrics.update({name: None for name in (
            "pct_missing_title", "pct_missing_meta_description", "pct_missing_h1",
            "pct_multiple_h1", "pct_missing_canonical", "pct_with_hreflang",
            "avg_word_count", "median_word_count", "avg_depth",
        )})
        return metrics

    metrics.update({
        "pct_missing_title": _pct(
            sum(not (page.title and page.title.strip()) for page in html_pages), total
        ),
        "pct_missing_meta_description": _pct(
            sum(not (page.meta_description and page.meta_description.strip())
                for page in html_pages), total
        ),
        "pct_missing_h1": _pct(
            sum(not any(h.strip() for h in page.h1) for page in html_pages), total
        ),
        "pct_multiple_h1": _pct(
            sum(sum(bool(h.strip()) for h in page.h1) > 1 for page in html_pages), total
        ),
        "pct_missing_canonical": _pct(
            sum(not (page.canonical and page.canonical.strip()) for page in html_pages), total
        ),
        "pct_with_hreflang": _pct(
            sum(page.hreflang_count > 0 for page in html_pages), total
        ),
        "avg_word_count": (
            round(sum(word_counts) / len(word_counts), 2) if word_counts else None
        ),
        "median_word_count": median(word_counts) if word_counts else None,
        "avg_depth": round(sum(page.depth for page in html_pages) / total, 2),
    })
    return metrics


def _url_structure(site: SiteSnapshot) -> dict:
    parsed = [urlsplit(page.url) for page in site.pages]
    depths = [len([part for part in parsed_url.path.split("/") if part]) for parsed_url in parsed]
    directories = Counter(
        parts[0]
        for parsed_url in parsed
        if (parts := [part for part in parsed_url.path.split("/") if part])
    )
    non_root_paths = [parsed_url.path for parsed_url in parsed if parsed_url.path != "/"]
    return {
        "label": site.label,
        "avg_path_depth": round(sum(depths) / len(depths), 2) if depths else 0.0,
        "top_directories": [
            {"dir": directory, "count": count}
            for directory, count in sorted(directories.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
        "pct_with_query": _pct(
            sum(bool(parsed_url.query) for parsed_url in parsed), len(parsed)
        ),
        "trailing_slash_pct": _pct(
            sum(path.endswith("/") for path in non_root_paths), len(non_root_paths)
        ),
        "uses_lang_prefix": any(
            (parts := [part.lower() for part in parsed_url.path.split("/") if part])
            and parts[0] in _LANG_PREFIX
            for parsed_url in parsed
        ),
    }


def _page_type_counts(site: SiteSnapshot) -> dict[str, int]:
    counts = Counter(classify_page(page) for page in _html_200(site.pages))
    return dict(sorted(counts.items()))


def _technical_differences(technical: list[dict]) -> list[dict]:
    if not technical:
        return []
    own = technical[0]
    differences: list[dict] = []
    for competitor in technical[1:]:
        for metric in _PERCENT_METRICS:
            own_value, competitor_value = own[metric], competitor[metric]
            if (own_value is None or competitor_value is None
                    or abs(own_value - competitor_value) < 15):
                continue
            differences.append({
                "metric": metric,
                "own": own_value,
                "competitor_label": competitor["label"],
                "competitor": competitor_value,
                "note": (
                    "Observed crawl-sample difference only; this is not evidence "
                    "that the metric causes ranking performance."
                ),
            })
        if own["sitemap_found"] != competitor["sitemap_found"]:
            differences.append({
                "metric": "sitemap_found",
                "own": own["sitemap_found"],
                "competitor_label": competitor["label"],
                "competitor": competitor["sitemap_found"],
                "note": (
                    "Observed crawl-sample difference only; this is not evidence "
                    "that the metric causes ranking performance."
                ),
            })
    return sorted(
        differences,
        key=lambda item: (item["competitor_label"], item["metric"]),
    )


def _missing_page_types(sites: list[SiteSnapshot], counts: list[dict[str, int]]) -> list[dict]:
    own_counts = counts[0] if counts else {}
    missing: list[dict] = []
    all_types = sorted({page_type for site_counts in counts[1:] for page_type in site_counts})
    for page_type in all_types:
        if own_counts.get(page_type, 0) or page_type in _IGNORED_MISSING_TYPES:
            continue
        examples: list[dict] = []
        for site, site_counts in zip(sites[1:], counts[1:]):
            if site_counts.get(page_type, 0) < 2:
                continue
            sample_urls = sorted(
                page.url for page in _html_200(site.pages)
                if classify_page(page) == page_type
            )[:3]
            examples.append({
                "label": site.label,
                "count": site_counts[page_type],
                "sample_urls": sample_urls,
            })
        if examples:
            missing.append({"page_type": page_type, "competitors": examples})
    return missing


def compare_sites(own: SiteSnapshot, competitors: list[SiteSnapshot]) -> dict:
    """Compare crawl snapshots descriptively; avoid implying ranking causation."""
    sites = [own, *competitors]
    technical = [_technical(site) for site in sites]
    counts = [_page_type_counts(site) for site in sites]
    return {
        "technical": technical,
        "technical_differences": _technical_differences(technical),
        "url_structure": [_url_structure(site) for site in sites],
        "page_types": [
            {"label": site.label, "page_types": site_counts}
            for site, site_counts in zip(sites, counts)
        ],
        "missing_page_types": _missing_page_types(sites, counts),
        "caveats": [
            "Crawls are samples limited by max_pages.",
            "Competitor presence does not imply a ranking advantage.",
        ],
    }
