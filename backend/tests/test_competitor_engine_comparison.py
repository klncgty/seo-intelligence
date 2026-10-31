from app.services.competitors.comparison import compare_sites
from app.services.competitors.types import PageSnapshot, SiteSnapshot


def page(url, **values):
    fields = {
        "status_code": 200,
        "is_html": True,
        "title": "Page title",
        "meta_description": "Description",
        "h1": ["Heading"],
        "canonical": f"https://example.test{url}",
        "hreflang_count": 0,
        "word_count": 100,
        "depth": 1,
        "in_sitemap": True,
        "internal_inlinks": 2,
    }
    fields.update(values)
    return PageSnapshot(url=f"https://example.test{url}", **fields)


def site(label, pages, *, sitemap=False, sitemap_count=0, rules=None):
    return SiteSnapshot(
        label=label,
        domain=f"{label}.example",
        base_url=f"https://{label}.example",
        pages=pages,
        sitemap_found=sitemap,
        sitemap_url_count=sitemap_count,
        findings_by_rule={} if rules is None else rules,
    )


def test_comparison_technical_metrics_and_no_html_values():
    own_pages = [
        page("/a", title=None, word_count=100, depth=1),
        page("/b", word_count=200, depth=2),
        page("/c", word_count=None, depth=3, h1=["First", "Second"]),
        page("/d", status_code=404),
    ]
    competitor_pages = [
        page("/a", title=None, canonical=None, word_count=50),
        page("/b", title=None, canonical=None, word_count=150),
        page("/c", word_count=250),
        page("/d", status_code=503),
    ]
    result = compare_sites(
        site("own", own_pages, sitemap=False, rules={"missing_title": 1}),
        [site("rival", competitor_pages, sitemap=True, sitemap_count=12)],
    )
    own, rival = result["technical"]

    assert own["pages_crawled"] == 4
    assert own["html_200_pages"] == 3
    assert own["pct_missing_title"] == 33.33
    assert own["pct_multiple_h1"] == 33.33
    assert own["avg_word_count"] == 150
    assert own["median_word_count"] == 150
    assert own["avg_depth"] == 2
    assert own["pct_non_200"] == 25
    assert own["findings_by_rule"] == {"missing_title": 1}
    assert rival["pct_missing_title"] == 66.67
    assert rival["pct_missing_canonical"] == 66.67
    assert rival["sitemap_url_count"] == 12
    assert {item["metric"] for item in result["technical_differences"]} == {
        "pct_missing_title", "pct_multiple_h1", "pct_missing_canonical", "sitemap_found"
    }
    assert all("not evidence" in item["note"] for item in result["technical_differences"])

    empty = compare_sites(site("empty", [page("/bad", status_code=500)]), [])
    for metric in (
        "pct_missing_title", "pct_missing_meta_description", "pct_missing_h1",
        "pct_multiple_h1", "pct_missing_canonical", "pct_with_hreflang",
        "avg_word_count", "median_word_count", "avg_depth",
    ):
        assert empty["technical"][0][metric] is None


def test_comparison_difference_threshold_and_site_structures():
    own_pages = [
        page("/", depth=0),
        page("/blog/a", depth=1),
        page("/tr/products/item/?ref=x", depth=2),
        page("/blog/b/", depth=2),
    ]
    near_pages = [
        page("/a", title=None),
        page("/b"),
        page("/c"),
        page("/d"),
        page("/e"),
        page("/f"),
        page("/g"),
    ]
    own = site("own", own_pages)
    near = site("near", near_pages, sitemap=False)
    structure = compare_sites(own, [near])

    # 1/7 missing title is under the 15-point materiality threshold.
    assert not any(
        diff["metric"] == "pct_missing_title"
        for diff in structure["technical_differences"]
    )
    own_urls = structure["url_structure"][0]
    assert own_urls["avg_path_depth"] == 1.75
    assert own_urls["pct_with_query"] == 25
    assert own_urls["trailing_slash_pct"] == 66.67
    assert own_urls["uses_lang_prefix"] is True
    assert own_urls["top_directories"] == [
        {"dir": "blog", "count": 2},
        {"dir": "tr", "count": 1},
    ]


def test_missing_page_types_require_two_competitor_pages_and_include_samples():
    own = site("own", [page("/")])
    rival = site("rival", [
        page("/integrations/slack", title="Slack integration"),
        page("/integrations/hubspot", title="HubSpot integration"),
        page("/blog/story-1", title="Article"),
        page("/blog/story-2", title="Article"),
        page("/login", title="Login"),
        page("/terms", title="Terms of service"),
        page("/misc", title="Miscellaneous"),
    ])
    other = site("other", [
        page("/integrations/one", title="Integration"),
    ])
    result = compare_sites(own, [rival, other])

    missing = {item["page_type"]: item for item in result["missing_page_types"]}
    assert missing["integration"]["competitors"] == [{
        "label": "rival",
        "count": 2,
        "sample_urls": [
            "https://example.test/integrations/hubspot",
            "https://example.test/integrations/slack",
        ],
    }]
    assert missing["blog_article"]["competitors"][0]["count"] == 2
    assert "login_account" not in missing
    assert "legal" not in missing
    assert "other" not in missing
    assert len(result["page_types"]) == 3
    assert result["caveats"] == [
        "Crawls are samples limited by max_pages.",
        "Competitor presence does not imply a ranking advantage.",
    ]
