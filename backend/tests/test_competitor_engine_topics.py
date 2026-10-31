from app.services.competitors.page_types import classify_page
from app.services.competitors.topics import content_gaps, extract_topics, tokenize
from app.services.competitors.types import OwnQuery, PageSnapshot, SiteSnapshot


def page(url, title=None, h1=None, status=200, html=True):
    return PageSnapshot(
        url=url,
        status_code=status,
        is_html=html,
        title=title,
        meta_description=None,
        h1=[] if h1 is None else [h1],
    )


def site(label, pages):
    return SiteSnapshot(label, f"{label}.example", f"https://{label}.example", pages)


def test_page_type_classification_table():
    cases = [
        ("https://x.example/", "Acme", "Acme", "home"),
        ("https://x.example/blog", "Company Blog", None, "blog_index"),
        ("https://x.example/blog/seo-checklist", "SEO checklist", None, "blog_article"),
        ("https://x.example/product/analytics", "Analytics product", None, "product"),
        ("https://x.example/category/shoes", "Shoes", None, "category"),
        ("https://x.example/pricing", "Plans and pricing", None, "pricing"),
        ("https://x.example/docs/api", "API documentation", None, "docs"),
        ("https://x.example/compare/a-vs-b", "Tool comparison", None, "comparison"),
        ("https://x.example/alternatives/tool", "Alternatives", None, "alternatives"),
        ("https://x.example/integrations/slack", "Slack integration", None, "integration"),
        ("https://x.example/case-studies/acme", "Customer story", None, "case_study"),
        ("https://x.example/features/analytics", "Analytics feature", None, "landing/feature"),
        ("https://x.example/faq", "FAQ", None, "faq"),
        ("https://x.example/glossary", "Glossary", None, "glossary"),
        ("https://x.example/hakkimizda", "Hakkımızda", None, "about"),
        ("https://x.example/contact", "Contact", None, "contact"),
        ("https://x.example/privacy-policy", "Privacy policy", None, "legal"),
        ("https://x.example/careers", "Jobs", None, "careers"),
        ("https://x.example/giris", "Giriş", None, "login_account"),
        ("https://x.example/unknown-path", "Overview", None, "other"),
        ("https://x.example/unknown", "Karşılaştırma rehberi", None, "comparison"),
    ]
    for url, title, h1, expected in cases:
        assert classify_page(page(url, title, h1)) == expected, url


def test_tokenize_english_turkish_stopwords_and_light_plural_stemming():
    assert tokenize("The analytics tools for ölçümler ve SEO") == [
        "analytic", "tool", "ölçümler", "seo"
    ]
    assert tokenize("customers dashboards and reports") == [
        "customer", "dashboard", "report"
    ]
    assert tokenize("a 12 99 ab xylophone") == ["xylophone"]


def test_extract_topics_requires_two_pages_or_slug_presence():
    pages = [
        page("https://x.example/analytics", "Analytics guide"),
        page("https://x.example/resources/analytics", "Analytics resources"),
        page("https://x.example/one-off", "Unique topic"),
        page("https://x.example/blocked", "Analytics", status=404),
        page("https://x.example/file.pdf", "Analytics", html=False),
        page("https://x.example/login", "Analytics login"),
    ]
    topics = extract_topics(pages)
    assert topics["analytic"] == [
        "https://x.example/analytics",
        "https://x.example/resources/analytics",
    ]
    assert "unique" not in topics
    assert "topic" not in topics


def test_extract_topics_excludes_login_legal_contact_and_careers():
    pages = [
        page("https://x.example/account", "Account"),
        page("https://x.example/legal/privacy", "Privacy policy"),
        page("https://x.example/contact", "Contact us"),
        page("https://x.example/careers", "Careers"),
    ]
    assert extract_topics(pages) == {}


def test_content_gaps_prioritizes_own_demand_and_matches_all_query_tokens():
    own = site("own", [page("https://own.example/", "Company home")])
    competitor_a = site("rival-a", [
        page("https://rival-a.example/seo-audit", "SEO audit checklist"),
        page("https://rival-a.example/seo-audit-tool", "SEO audit tool"),
        page("https://rival-a.example/keyword-research", "Keyword research"),
    ])
    competitor_b = site("rival-b", [
        page("https://rival-b.example/keyword-research-guide", "Keyword research"),
    ])
    queries = [
        OwnQuery("best SEO audit software", 4, 80, 8.0),
        OwnQuery("keyword research", 2, 20, 12.0),
        OwnQuery("SEO strategy", 1, 200, 4.0),
    ]
    gaps = content_gaps(own, [competitor_a, competitor_b], queries)

    assert [gap["topic"] for gap in gaps[:3]] == ["seo", "audit", "seo audit"]
    assert next(i for i, gap in enumerate(gaps) if gap["topic"] == "keyword research") > 2
    audit = next(gap for gap in gaps if gap["topic"] == "seo audit")
    assert audit["evidence_level"] == "own_demand"
    assert audit["own_gsc_evidence"] == {
        "matched_queries": [
            {"query": "best SEO audit software", "impressions": 80, "clicks": 4}
        ],
        "impressions": 80,
        "clicks": 4,
    }
    keyword = next(gap for gap in gaps if gap["topic"] == "keyword research")
    assert keyword["evidence_level"] == "own_demand"
    assert len(keyword["competitors"]) == 2
    assert keyword["own_gsc_evidence"]["impressions"] == 20
def test_content_gaps_without_matching_gsc_is_competitor_only_and_sorted():
    own = site("own", [])
    competitor = site("rival", [
        page("https://rival.example/analytics", "Analytics"),
        page("https://rival.example/conversion", "Conversion guide"),
    ])
    gaps = content_gaps(
        own, [competitor], [OwnQuery("analytics tools", 0, 0, 40.0)]
    )
    assert all(gap["evidence_level"] == "competitor_only" for gap in gaps)
    assert any(
        gap["own_gsc_evidence"] is not None
        and gap["own_gsc_evidence"]["impressions"] == 0
        for gap in gaps
    )
    assert [gap["topic"] for gap in gaps] == sorted(gap["topic"] for gap in gaps)


def test_content_gap_urls_are_capped_and_deterministic():
    own = site("own", [])
    pages = [
        page(f"https://rival.example/guide/topic-{index}", "Topic")
        for index in range(7)
    ]
    gaps = content_gaps(own, [site("rival", pages)], [])
    guide_topic = next(gap for gap in gaps if gap["topic"] == "topic")
    competitor = guide_topic["competitors"][0]
    assert competitor["page_count"] == 7
    assert len(competitor["urls"]) == 5
    assert competitor["urls"] == sorted(competitor["urls"])[:5]
