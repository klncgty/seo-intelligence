from app.services.competitors.opportunities import competitor_opportunities
from app.services.competitors.types import SiteSnapshot


def site(label, crawl_id):
    return SiteSnapshot(
        label=label,
        domain=f"{label}.example",
        base_url=f"https://{label}.example",
        crawl_id=crawl_id,
    )


def test_opportunity_evidence_rules_sources_impact_and_limitations():
    own = site("own", 10)
    rival_a = site("rival-a", 20)
    rival_b = site("rival-b", 30)
    gaps = [
        {
            "topic": "seo audit",
            "evidence_level": "own_demand",
            "competitors": [{"label": "rival-a", "urls": [], "page_count": 1}],
            "own_gsc_evidence": {
                "matched_queries": [{"query": "seo audit", "impressions": 70, "clicks": 3}],
                "impressions": 70,
                "clicks": 3,
            },
        },
        {
            "topic": "keyword research",
            "evidence_level": "competitor_only",
            "competitors": [
                {"label": "rival-a", "urls": [], "page_count": 2},
                {"label": "rival-b", "urls": [], "page_count": 1},
            ],
            "own_gsc_evidence": None,
        },
        {
            "topic": "single competitor only",
            "evidence_level": "competitor_only",
            "competitors": [{"label": "rival-a", "urls": [], "page_count": 1}],
            "own_gsc_evidence": None,
        },
    ]
    date_range = {"start": "2026-01-01", "end": "2026-01-31"}
    opportunities = competitor_opportunities(
        own, [rival_a, rival_b], gaps, [], [], date_range
    )

    assert {item.key for item in opportunities} == {
        "competitor_content_gap|seo audit",
        "competitor_content_gap|keyword research",
    }
    own_demand = next(item for item in opportunities if item.key.endswith("seo audit"))
    assert own_demand.confidence == 0.55
    assert own_demand.data_sources == ["crawl:own#10", "crawl:rival-a#20", "gsc:own"]
    assert own_demand.evidence["own_gsc_evidence"]["impressions"] == 70
    assert own_demand.date_range == date_range
    assert all(item.impact_estimate["value"] is None for item in opportunities)
    assert all(
        "A competitor having this does not prove it ranks better because of it."
        in item.limitations
        for item in opportunities
    )
    competitor_only = next(
        item for item in opportunities if item.key.endswith("keyword research")
    )
    assert competitor_only.confidence == 0.25
    assert competitor_only.data_sources == [
        "crawl:own#10", "crawl:rival-a#20", "crawl:rival-b#30"
    ]
    assert competitor_only.priority_score == 3.0
    assert "cannot quantify" in competitor_only.impact_estimate["method"]


def test_page_type_and_own_side_technical_opportunities():
    own = site("own", 1)
    rival = site("rival", 2)
    missing_types = [{
        "page_type": "integration",
        "competitors": [{
            "label": "rival",
            "count": 3,
            "sample_urls": ["https://rival.example/integrations"],
        }],
    }]
    technical_differences = [
        {
            "metric": "pct_missing_title",
            "own": 40,
            "competitor_label": "rival",
            "competitor": 20,
            "note": "sample only",
        },
        {
            "metric": "pct_missing_canonical",
            "own": 10,
            "competitor_label": "rival",
            "competitor": 30,
            "note": "own is better",
        },
        {
            "metric": "sitemap_found",
            "own": False,
            "competitor_label": "rival",
            "competitor": True,
            "note": "sample only",
        },
        {
            "metric": "pct_missing_title",
            "own": 25,
            "competitor_label": "rival",
            "competitor": 20,
            "note": "below the material difference threshold",
        },
    ]
    opportunities = competitor_opportunities(
        own, [rival], [], missing_types, technical_differences, {"start": "a", "end": "b"}
    )
    assert {item.key for item in opportunities} == {
        "competitor_page_type_gap|integration",
        "competitor_technical_gap|pct_missing_title",
        "competitor_technical_gap|sitemap_found",
    }
    assert all(item.confidence in {0.3, 0.4} for item in opportunities)
    assert all(item.impact_estimate["value"] is None for item in opportunities)
    sitemap = next(item for item in opportunities if item.key.endswith("sitemap_found"))
    assert sitemap.data_sources == ["crawl:own#1", "crawl:rival#2"]
    assert sitemap.priority_score == 8.4


def test_opportunity_order_is_repeatable_and_priority_uses_unknown_impact_factor():
    own = site("own", None)
    rival_a = site("rival-a", None)
    rival_b = site("rival-b", None)
    args = (
        own,
        [rival_b, rival_a],
        [
            {
                "topic": "zeta",
                "evidence_level": "competitor_only",
                "competitors": [{"label": "rival-a"}, {"label": "rival-b"}],
                "own_gsc_evidence": None,
            },
            {
                "topic": "alpha",
                "evidence_level": "competitor_only",
                "competitors": [{"label": "rival-a"}, {"label": "rival-b"}],
                "own_gsc_evidence": None,
            },
        ],
        [],
        [],
        {},
    )
    first = competitor_opportunities(*args)
    second = competitor_opportunities(*args)
    assert [item.key for item in first] == [item.key for item in second]
    assert [item.key for item in first] == [
        "competitor_content_gap|alpha",
        "competitor_content_gap|zeta",
    ]
    assert [item.priority_score for item in first] == [3.0, 3.0]
