import math
from datetime import date, timedelta

from app.services.growth import (
    Ga4Row, GrowthInput, GscRow, LinkGraph, PageIssue, Thresholds, generate_opportunities,
)
from app.services.growth.engine import normalize_path

CS, CE = date(2026, 3, 1), date(2026, 3, 30)
PS, PE = date(2026, 1, 30), date(2026, 2, 28)


def gsc(page, query, clicks, imps, pos, d=CS):
    return GscRow(d, page, query, "desktop", "us", clicks, imps, pos)


def ga4(page, sessions, signups=None, purchases=None, d=CS):
    return Ga4Row(d, page, "Organic Search", sessions, sessions, signups, purchases, None)


def mk(**kw):
    base = dict(site_id=1, base_url="https://x.com", current_start=CS, current_end=CE,
                previous_start=PS, previous_end=PE)
    base.update(kw)
    return GrowthInput(**base)


def of_type(opps, t):
    return [o for o in opps if o.opportunity_type == t]


def ctr_rows(low_page_clicks=2):
    rows = [gsc(f"/p{i}", f"q{i}", 30, 300, 5.0) for i in range(6)]  # ctr 0.10
    rows.append(gsc("/bad", "badq", low_page_clicks, 400, 5.0))
    return rows


def test_normalize():
    assert normalize_path("https://x.com/a/b/?x=1#f") == "/a/b"
    assert normalize_path("https://x.com") == "/"


def test_low_ctr_positive_and_impact():
    o = of_type(generate_opportunities(mk(gsc_rows=ctr_rows())), "low_ctr")
    assert [x.key for x in o] == ["low_ctr|/bad|badq"]
    # median of 6x0.10 and 0.005 = 0.10 ; (0.10-0.005)*400 = 38
    assert o[0].impact_estimate["value"] == 38.0
    assert o[0].data_sources == ["gsc"]


def test_low_ctr_negative_cases():
    assert not of_type(generate_opportunities(mk(gsc_rows=ctr_rows(low_page_clicks=30))), "low_ctr")
    # too few comparable rows in bucket
    rows = [gsc("/a", "a", 30, 300, 5.0), gsc("/bad", "b", 1, 400, 5.0)]
    assert not of_type(generate_opportunities(mk(gsc_rows=rows)), "low_ctr")
    # position > 20 skipped
    rows = [gsc(f"/p{i}", f"q{i}", 30, 300, 25.0) for i in range(6)] + [gsc("/bad", "b", 0, 400, 25.0)]
    assert not of_type(generate_opportunities(mk(gsc_rows=rows)), "low_ctr")


def test_low_ctr_threshold_respected():
    opps = generate_opportunities(mk(gsc_rows=ctr_rows()), Thresholds(min_impressions=500))
    assert not of_type(opps, "low_ctr")


def test_declining_with_and_without_yoy():
    rows = [gsc("/d", "q", 100, 1000, 3, PS), gsc("/d", "q", 60, 900, 3, CS)]
    o = of_type(generate_opportunities(mk(gsc_rows=rows)), "declining_performance")
    assert len(o) == 1 and o[0].evidence["drop_pct"] == 40.0
    assert o[0].evidence["clicks_year_ago"] is None
    assert "unknown" in o[0].limitations
    rows.append(gsc("/d", "q", 150, 1000, 3, CS - timedelta(days=365)))
    o = of_type(generate_opportunities(mk(gsc_rows=rows)), "declining_performance")
    assert o[0].evidence["clicks_year_ago"] == 150


def test_yoy_evidence_is_page_specific():
    rows = [
        gsc("/d", "q", 100, 1000, 3, PS),
        gsc("/d", "q", 50, 900, 3, CS),
        gsc("/other", "q", 150, 1000, 3, CS - timedelta(days=365)),
    ]
    o = of_type(generate_opportunities(mk(gsc_rows=rows)), "declining_performance")
    assert len(o) == 1
    assert o[0].evidence["clicks_year_ago"] is None
    assert "unknown" in o[0].limitations


def test_declining_negative():
    rows = [gsc("/d", "q", 100, 1000, 3, PS), gsc("/d", "q", 80, 900, 3, CS)]
    assert not of_type(generate_opportunities(mk(gsc_rows=rows)), "declining_performance")
    rows = [gsc("/d", "q", 40, 1000, 3, PS)]
    assert not of_type(generate_opportunities(mk(gsc_rows=rows)), "declining_performance")


def test_declining_confidence_by_volume():
    small = [gsc("/d", "q", 60, 1, 3, PS)]
    big = [gsc("/d", "q", 1000, 1, 3, PS)]
    c1 = of_type(generate_opportunities(mk(gsc_rows=small)), "declining_performance")[0].confidence
    c2 = of_type(generate_opportunities(mk(gsc_rows=big)), "declining_performance")[0].confidence
    assert c1 < c2


def test_indexing_issue():
    issues = [PageIssue("https://x.com/a/", "noindex_meta", "high"),
              PageIssue("https://x.com/b", "noindex_meta", "high"),
              PageIssue("https://x.com/c", "title_missing", "low")]
    rows = [gsc("/a", "q", 12, 100, 3), gsc("/b", "q", 2, 100, 3), gsc("/c", "q", 50, 100, 3)]
    o = of_type(generate_opportunities(mk(gsc_rows=rows, issues=issues)),
                "indexing_issue_on_valuable_page")
    assert [x.url for x in o] == ["/a"]
    # ga4 sessions / conversions qualify
    o = of_type(generate_opportunities(mk(issues=issues, ga4_rows=[ga4("/b", 25)],
                                          gsc_available=False)),
                "indexing_issue_on_valuable_page")
    assert [x.url for x in o] == ["/b"]
    o = of_type(generate_opportunities(mk(issues=issues, ga4_rows=[ga4("/b", 5, signups=1)],
                                          gsc_available=False)),
                "indexing_issue_on_valuable_page")
    assert [x.url for x in o] == ["/b"]


def conv_rows():
    return [ga4("/a", 200, 20), ga4("/b", 200, 20), ga4("/c", 200, 20), ga4("/low", 200, 1),
            ga4("/small", 50, 0)]


def test_low_conversion():
    o = of_type(generate_opportunities(mk(ga4_rows=conv_rows())), "low_conversion")
    assert [x.url for x in o] == ["/low"]
    # median of [.1,.1,.1,.005] = 0.1 -> 200*(0.1-0.005)=19
    assert o[0].impact_estimate["value"] == 19.0


def test_low_conversion_skips():
    unset = [ga4(p, 200) for p in "abcd"]
    assert not of_type(generate_opportunities(mk(ga4_rows=unset)), "low_conversion")
    assert not of_type(generate_opportunities(mk(ga4_rows=conv_rows(), ga4_available=False)),
                       "low_conversion")
    o = generate_opportunities(mk(ga4_rows=conv_rows()), Thresholds(min_sessions=300))
    assert not of_type(o, "low_conversion")


def test_weak_internal_linking():
    g = LinkGraph(inlinks={"https://x.com/a": 0, "https://x.com/b": 5, "https://x.com/c": 0},
                  depth={"https://x.com/a": 1, "https://x.com/b": 5, "https://x.com/c": 1})
    rows = [gsc("/a", "q", 10, 100, 3), gsc("/b", "q", 10, 100, 3), gsc("/c", "q", 9, 100, 3)]
    o = of_type(generate_opportunities(mk(gsc_rows=rows, graph=g)), "weak_internal_linking")
    assert sorted(x.url for x in o) == ["/a", "/b"]
    assert not of_type(generate_opportunities(mk(gsc_rows=rows, graph=None)),
                       "weak_internal_linking")


def test_weak_linking_does_not_use_ga4_when_unavailable():
    graph = LinkGraph(inlinks={"/a": 0}, depth={"/a": 1})
    rows = [ga4("/a", 1, signups=1)]
    assert not of_type(
        generate_opportunities(
            mk(ga4_rows=rows, graph=graph, gsc_available=False, ga4_available=False)
        ),
        "weak_internal_linking",
    )


def test_content_intent_cannibalisation_and_striking():
    rows = [gsc("/a", "shoes", 5, 400, 6), gsc("/b", "shoes", 5, 300, 7),
            gsc("/c", "shoes", 0, 10, 9),
            gsc("/s", "boots", 5, 800, 12)]
    o = of_type(generate_opportunities(mk(gsc_rows=rows)), "content_intent")
    by = {x.evidence["subtype"]: x for x in o}
    assert by["cannibalisation"].impact_estimate["value"] is None
    assert by["cannibalisation"].url is None
    assert {p["page"] for p in by["cannibalisation"].evidence["pages"]} == {"/a", "/b", "/c"}
    assert by["striking_distance"].url == "/s"
    assert by["striking_distance"].key == "content_intent|/s|boots"


def test_content_intent_negative():
    rows = [gsc("/a", "q", 5, 900, 6), gsc("/b", "q", 0, 50, 7),  # b share <20%
            gsc("/s", "low", 1, 100, 12)]
    assert not of_type(generate_opportunities(mk(gsc_rows=rows)), "content_intent")


def test_unavailable_sources_skip():
    rows = ctr_rows() + [gsc("/d", "q", 100, 1000, 3, PS)]
    o = generate_opportunities(mk(gsc_rows=rows, ga4_rows=conv_rows(), gsc_available=False))
    types = {x.opportunity_type for x in o}
    assert not types & {"low_ctr", "declining_performance", "content_intent"}
    assert "low_conversion" in types
    o = generate_opportunities(mk(gsc_rows=rows, ga4_rows=conv_rows(), ga4_available=False))
    assert "low_conversion" not in {x.opportunity_type for x in o}


def test_priority_formula_and_order():
    rows = ctr_rows() + [gsc("/d", "q", 100, 1000, 3, PS), gsc("/d", "q", 50, 900, 3, CS)]
    o = generate_opportunities(mk(gsc_rows=rows))
    ctr = of_type(o, "low_ctr")[0]
    dec = of_type(o, "declining_performance")[0]
    # impacts: ctr 38 (conf .5+.04=.54, low), decline 50 (conf .65, medium)
    assert dec.priority_score == round(1.0 * 0.65 * 0.7 * 100, 2)
    exp = math.log10(39) / math.log10(51) * ctr.confidence * 1.0 * 100
    assert ctr.priority_score == round(exp, 2)
    keys = [(-x.priority_score, x.key) for x in o]
    assert keys == sorted(keys)


def test_unknown_impact_norm_and_determinism():
    rows = [gsc("/a", "shoes", 5, 400, 6), gsc("/b", "shoes", 5, 300, 7)]
    o = generate_opportunities(mk(gsc_rows=rows))
    assert o[0].priority_score == round(0.3 * 0.55 * 0.7 * 100, 2)
    inp = mk(gsc_rows=ctr_rows() + rows)
    assert generate_opportunities(inp) == generate_opportunities(inp)
