"""Convert competitor observations into cautious, deterministic opportunities."""
from __future__ import annotations

from app.services.competitors.types import SiteSnapshot
from app.services.growth.types import Opportunity

_LIMITATION = "A competitor having this does not prove it ranks better because of it."
_EFFORT_FACTOR = {"low": 1.0, "medium": 0.7, "high": 0.4}


def _crawl_source(label: str, site: SiteSnapshot | None) -> str:
    if site is None or site.crawl_id is None:
        return f"crawl:{label}"
    return f"crawl:{label}#{site.crawl_id}"


def _new_opportunity(
    *,
    key: str,
    opportunity_type: str,
    title: str,
    description: str,
    evidence: dict,
    data_sources: list[str],
    date_range: dict[str, str],
    confidence: float,
    effort: str,
    action: str,
    validation_metric: str,
) -> Opportunity:
    impact_estimate = {
        "metric": "unknown",
        "value": None,
        "unit": "n/a",
        "method": (
            "Unknown: crawl comparisons and competitor/provider estimates cannot "
            "quantify this site's incremental traffic or business impact."
        ),
    }
    # All competitor opportunities have unknown impact, so impact_norm is 0.3.
    priority_score = round(
        0.3 * confidence * _EFFORT_FACTOR[effort] * 100,
        2,
    )
    return Opportunity(
        key=key,
        opportunity_type=opportunity_type,
        url=None,
        title=title,
        description=description,
        evidence=evidence,
        data_sources=list(dict.fromkeys(data_sources)),
        date_range=dict(date_range),
        impact_estimate=impact_estimate,
        confidence=confidence,
        effort=effort,
        priority_score=priority_score,
        recommended_action=action,
        validation_metric=validation_metric,
        limitations=_LIMITATION,
    )


def competitor_opportunities(
    own: SiteSnapshot,
    competitors: list[SiteSnapshot],
    gaps: list[dict],
    missing_page_types: list[dict],
    technical_differences: list[dict],
    date_range: dict[str, str],
) -> list[Opportunity]:
    """Create opportunities without treating competitor estimates as own analytics."""
    competitors_by_label = {site.label: site for site in competitors}
    own_source = _crawl_source("own", own)
    opportunities: list[Opportunity] = []

    for gap in gaps:
        topic = gap.get("topic")
        if not isinstance(topic, str) or not topic:
            continue
        competitor_rows = gap.get("competitors", [])
        if not isinstance(competitor_rows, list):
            continue
        distinct_labels = sorted({
            row.get("label")
            for row in competitor_rows
            if isinstance(row, dict) and isinstance(row.get("label"), str)
        })
        own_evidence = gap.get("own_gsc_evidence")
        evidence_level = gap.get("evidence_level")
        if evidence_level != "own_demand" and len(distinct_labels) < 2:
            continue
        sources = [own_source]
        sources.extend(
            _crawl_source(label, competitors_by_label.get(label))
            for label in distinct_labels
        )
        if own_evidence is not None:
            sources.append("gsc:own")
        confidence = 0.55 if evidence_level == "own_demand" else 0.25
        opportunities.append(_new_opportunity(
            key=f"competitor_content_gap|{topic}",
            opportunity_type="competitor_content_gap",
            title=f"Create content for '{topic}'",
            description=(
                f"{len(distinct_labels)} competitor site(s) have pages about '{topic}', "
                "while the topic is absent from the own-site crawl."
            ),
            evidence={
                "topic": topic,
                "evidence_level": evidence_level,
                "competitors": competitor_rows,
                "own_gsc_evidence": own_evidence,
            },
            data_sources=sources,
            date_range=date_range,
            confidence=confidence,
            effort="high",
            action="Validate search intent, then create an original page that fills the observed topic gap.",
            validation_metric=f"Own-site impressions and clicks for '{topic}' in GSC",
        ))

    for item in missing_page_types:
        page_type = item.get("page_type")
        if not isinstance(page_type, str) or not page_type:
            continue
        competitor_rows = item.get("competitors", [])
        if not isinstance(competitor_rows, list):
            continue
        labels = sorted({
            row.get("label")
            for row in competitor_rows
            if isinstance(row, dict) and isinstance(row.get("label"), str)
        })
        sources = [own_source]
        sources.extend(
            _crawl_source(label, competitors_by_label.get(label)) for label in labels
        )
        opportunities.append(_new_opportunity(
            key=f"competitor_page_type_gap|{page_type}",
            opportunity_type="competitor_page_type_gap",
            title=f"Assess a {page_type.replace('_', ' ')} page",
            description=(
                f"The own-site crawl has no {page_type.replace('_', ' ')} pages; "
                "one or more competitors have multiple examples."
            ),
            evidence={"page_type": page_type, "competitors": competitor_rows},
            data_sources=sources,
            date_range=date_range,
            confidence=0.3,
            effort="high",
            action="Assess whether this page type serves an own-customer need before producing a page.",
            validation_metric=f"Qualified organic visits to an own {page_type} page",
        ))

    for difference in technical_differences:
        metric = difference.get("metric")
        competitor_label = difference.get("competitor_label")
        own_value = difference.get("own")
        competitor_value = difference.get("competitor")
        if not isinstance(competitor_label, str):
            continue
        is_deficit = (
            metric in {"pct_missing_title", "pct_missing_canonical"}
            and isinstance(own_value, (int, float))
            and isinstance(competitor_value, (int, float))
            and own_value - competitor_value >= 15
        ) or (
            metric == "sitemap_found"
            and own_value is False
            and competitor_value is True
        )
        if not is_deficit:
            continue
        competitor_site = competitors_by_label.get(competitor_label)
        sources = [own_source, _crawl_source(competitor_label, competitor_site)]
        if metric == "sitemap_found":
            action = "Verify sitemap availability and coverage for the own site's important URLs."
            description = "The own-site crawl did not find a sitemap; this competitor's crawl did."
        else:
            action = (
                "Review affected own-site pages and correct missing metadata where appropriate."
            )
            description = (
                f"Observed {own_value:.2f}% on the own site versus "
                f"{competitor_value:.2f}% for {competitor_label}."
            )
        opportunities.append(_new_opportunity(
            key=f"competitor_technical_gap|{metric}",
            opportunity_type="competitor_technical_gap",
            title=f"Review own-site {metric.replace('_', ' ')}",
            description=description,
            evidence={
                "metric": metric,
                "own": own_value,
                "competitor_label": competitor_label,
                "competitor": competitor_value,
                "observation": difference.get("note"),
            },
            data_sources=sources,
            date_range=date_range,
            confidence=0.4,
            effort="medium",
            action=action,
            validation_metric=f"{metric} in the next own-site crawl",
        ))

    return sorted(opportunities, key=lambda item: (-item.priority_score, item.key))
