from __future__ import annotations

from app.services.audit import RULES, run_audit
from app.services.audit.types import AuditInput, Finding, LinkInput, PageInput

BASE = "https://example.test/"


def page(url: str, **values: object) -> PageInput:
    defaults: dict[str, object] = {
        "status_code": 200,
        "content_type": "text/html",
        "is_html": True,
        "titles": [f"Title {url}"],
        "meta_descriptions": [f"Description {url}"],
        "h1": [f"Heading {url}"],
        "canonicals": [url],
        "depth": 1,
    }
    defaults.update(values)
    return PageInput(url=url, **defaults)  # type: ignore[arg-type]


def comprehensive_input() -> AuditInput:
    urls = {
        name: f"https://example.test/{name}"
        for name in (
            "root", "bad4", "bad5", "failed", "asset", "title-missing",
            "title-multiple", "description-missing", "description-multiple",
            "h1-missing", "h1-multiple", "canonical-missing", "canonical-multiple",
            "canonical-invalid", "redirect-target", "canonical-other", "noindex-meta",
            "noindex-header", "lang-source", "lang-target", "canonical-conflict",
            "chain", "loop", "redirect-link", "orphan", "not-in-sitemap",
            "thin", "duplicate-title-a", "duplicate-title-b", "duplicate-desc-a",
            "duplicate-desc-b", "deep", "few-inlinks",
            "unminified", "dup-h1-title", "missing-anchor", "internal-nofollow",
        )
    }
    pages = [
        page(urls["root"], depth=0),
        page(urls["bad4"], status_code=404),
        page(urls["bad5"], status_code=503),
        page(urls["failed"], status_code=None, fetch_error="timeout"),
        page(urls["asset"], is_html=False, content_type="application/pdf"),
        page(urls["title-missing"], titles=[]),
        page(urls["title-multiple"], titles=["First", "Second"]),
        page(urls["description-missing"], meta_descriptions=[]),
        page(urls["description-multiple"], meta_descriptions=["First", "Second"]),
        page(urls["h1-missing"], h1=[]),
        page(urls["h1-multiple"], h1=["First", "Second"]),
        page(urls["canonical-missing"], canonicals=[]),
        page(urls["canonical-multiple"], canonicals=[BASE, urls["root"]]),
        page(urls["canonical-invalid"], canonicals=[urls["redirect-target"]]),
        page(urls["redirect-target"], status_code=302, redirect_chain=[(urls["redirect-target"], 302)]),
        page(urls["canonical-other"], canonicals=[urls["root"]]),
        page(urls["noindex-meta"], meta_robots="noindex"),
        page(urls["noindex-header"], x_robots_tag="noindex"),
        page(
            urls["lang-source"],
            hreflang=[
                ("bad_code!", urls["lang-target"]),
                ("en", urls["lang-target"]),
                ("en", urls["bad4"]),
                ("fr", urls["canonical-conflict"]),
            ],
        ),
        page(urls["lang-target"]),
        page(urls["canonical-conflict"], canonicals=[urls["root"]]),
        page(urls["chain"], status_code=301, redirect_chain=[("/a", 301), ("/b", 302)]),
        page(urls["loop"], status_code=301, redirect_chain=[("/loop", 301), ("/loop", 301)]),
        page(urls["redirect-link"], status_code=301, redirect_chain=[("/redirect-link", 301)]),
        page(urls["orphan"]),
        page(urls["not-in-sitemap"]),
        page(urls["thin"], word_count=149),
        page(urls["duplicate-title-a"], titles=["Shared title"]),
        page(urls["duplicate-title-b"], titles=["Shared title"]),
        page(urls["duplicate-desc-a"], meta_descriptions=["Shared description"]),
        page(urls["duplicate-desc-b"], meta_descriptions=["Shared description"]),
        page(urls["deep"], depth=4),
        page(urls["few-inlinks"], depth=2),
        page(urls["unminified"], unminified_resources=["/assets/app.js"]),
        page(urls["dup-h1-title"], titles=["Same Heading"], h1=["Same Heading"]),
        page(urls["missing-anchor"]),
        page(urls["internal-nofollow"]),
    ]
    links = [
        LinkInput(
            urls["root"],
            p.url,
            anchor_text="" if p.url == urls["missing-anchor"] else "Anchor",
            rel="nofollow" if p.url == urls["internal-nofollow"] else None,
        )
        for p in pages
        if p.url not in (urls["root"], urls["orphan"])
    ]
    sitemap = {p.url for p in pages if p.is_html and p.status_code == 200}
    sitemap.remove(urls["not-in-sitemap"])
    sitemap.add("https://example.test/not-crawled")
    sitemap.add("https://example.test/robots-blocked")
    return AuditInput(
        base_url=BASE,
        pages=pages,
        links=links,
        sitemap_urls=sitemap,
        robots_blocked_urls={"https://example.test/robots-blocked"},
        sitemap_found=True,
        llms_txt_found=False,
    )


def ids(findings: list[Finding]) -> set[str]:
    return {f.rule_id for f in findings}


def test_every_registered_rule_has_positive_and_negative_coverage() -> None:
    positive = run_audit(comprehensive_input())
    assert ids(positive) == {rule.rule_id for rule in RULES}
    assert run_audit(AuditInput(base_url=BASE)) == []


def test_audit_is_deterministic_and_deduplicates_identical_findings() -> None:
    inp = comprehensive_input()
    first = run_audit(inp)
    assert first == run_audit(inp)
    page_input = page("https://example.test/dup", status_code=404)
    duplicate = Finding(
        "custom", "low", page_input.url, "Repeated", "Repeated", {"x": 1}, "Fix"
    )

    def repeated_rule(_: AuditInput):
        return [duplicate, duplicate]

    repeated_rule.rule_id = "custom"  # type: ignore[attr-defined]
    repeated_rule.severity = "low"  # type: ignore[attr-defined]
    original_rules = RULES[:]
    try:
        RULES.append(repeated_rule)
        assert run_audit(AuditInput(base_url=BASE)) == [duplicate]
    finally:
        RULES[:] = original_rules


def test_noindex_severity_depends_on_sitemap_or_internal_links() -> None:
    listed = page("https://example.test/listed", meta_robots="noindex")
    isolated = page("https://example.test/isolated", meta_robots="noindex")
    findings = run_audit(
        AuditInput(
            base_url=BASE,
            pages=[listed, isolated],
            sitemap_urls={listed.url},
        )
    )
    by_url = {f.url: f.severity for f in findings if f.rule_id == "noindex_meta"}
    assert by_url == {listed.url: "high", isolated.url: "medium"}


def test_non_200_canonical_and_hreflang_targets_are_invalid() -> None:
    source = page(
        "https://example.test/source",
        canonicals=["https://example.test/canonical-201"],
        hreflang=[("en", "https://example.test/alternate-204")],
    )
    canonical_201 = page("https://example.test/canonical-201", status_code=201)
    alternate_204 = page("https://example.test/alternate-204", status_code=204)
    findings = run_audit(
        AuditInput(
            base_url=BASE,
            pages=[source, canonical_201, alternate_204],
        )
    )
    assert "canonical_invalid_target" in ids(findings)
    assert "hreflang_invalid_target" in ids(findings)


def test_internal_link_to_redirect_detects_unrecorded_3xx_status() -> None:
    source = page("https://example.test/source")
    target = page("https://example.test/target", status_code=302)
    findings = run_audit(
        AuditInput(
            base_url=BASE,
            pages=[source, target],
            links=[LinkInput(source.url, target.url)],
        )
    )
    assert "internal_link_to_redirect" in ids(findings)


def test_semrush_grade_audit_rules() -> None:
    p1 = page("https://example.test/page1", titles=["Same Text"], h1=["Same Text"],
              unminified_resources=["https://example.test/styles.css"])
    p2 = page("https://example.test/page2", titles=["Distinct Title"], h1=["Different Heading"])
    findings = run_audit(
        AuditInput(
            base_url=BASE,
            pages=[p1, p2],
            links=[LinkInput(p1.url, p2.url, is_internal=True, anchor_text="   ", rel="nofollow")],
            llms_txt_found=False,
        )
    )
    rule_ids = ids(findings)
    assert "unminified_resources" in rule_ids
    assert "duplicate_h1_and_title" in rule_ids
    assert "link_missing_anchor_text" in rule_ids
    assert "llms_txt_missing" in rule_ids
    assert "internal_link_has_nofollow" in rule_ids

    # When resolved, none should trigger
    clean_p1 = page("https://example.test/page1", titles=["Keyword Title"], h1=["Main Heading"],
                    unminified_resources=[])
    resolved = run_audit(
        AuditInput(
            base_url=BASE,
            pages=[clean_p1, p2],
            links=[LinkInput(clean_p1.url, p2.url, is_internal=True, anchor_text="Explore More", rel=None)],
            llms_txt_found=True,
        )
    )
    clean_ids = ids(resolved)
    assert "unminified_resources" not in clean_ids
    assert "duplicate_h1_and_title" not in clean_ids
    assert "link_missing_anchor_text" not in clean_ids
    assert "llms_txt_missing" not in clean_ids
    assert "internal_link_has_nofollow" not in clean_ids
    assert "llms_txt_missing" not in clean_ids
