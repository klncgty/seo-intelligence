"""Deterministic growth opportunity engine (pure functions, site's own data only).

Priority formula::

    priority = impact_norm * confidence * effort_factor * 100
    impact_norm = log10(1+impact_value) / log10(1+max_impact_in_batch)
                  (0.3 when impact value is unknown)
    effort_factor: low=1.0, medium=0.7, high=0.4

Rounded to 2 decimals. Output is sorted by priority desc, then key.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from urllib.parse import urlsplit

from app.services.growth.types import GrowthInput, GscRow, Opportunity

PRIORITY_FORMULA = (
    "priority = impact_norm * confidence * effort_factor * 100; "
    "impact_norm = log10(1+impact_value)/log10(1+max_impact_in_batch) "
    "(0.3 when impact unknown); effort_factor low=1.0 medium=0.7 high=0.4"
)
EFFORT_FACTOR = {"low": 1.0, "medium": 0.7, "high": 0.4}
INDEXING_RULES = frozenset({
    "noindex_meta", "noindex_header", "canonical_invalid_target", "canonical_multiple",
    "http_4xx", "http_5xx", "redirect_loop", "robots_blocked", "fetch_failed",
})


@dataclass(frozen=True)
class Thresholds:
    """Tunable thresholds; defaults follow the product spec."""

    min_impressions: int = 200
    low_ctr_ratio: float = 0.5
    min_bucket_rows: int = 5
    max_position: float = 20.0
    decline_min_previous_clicks: int = 50
    decline_pct: float = 0.30
    indexing_min_clicks: int = 10
    indexing_min_sessions: int = 20
    min_sessions: int = 100
    low_conversion_ratio: float = 0.5
    min_pages_for_median: int = 3
    linking_min_clicks: int = 10
    max_inlinks: int = 1
    min_depth: int = 4
    cannibal_share: float = 0.20
    striking_min_position: float = 8.0
    striking_max_position: float = 20.0
    striking_min_impressions: int = 500


def normalize_path(url: str) -> str:
    """Strip scheme/host/query/fragment/trailing slash; '' becomes '/'."""
    path = urlsplit(url.strip()).path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _in(d: date, start: date, end: date) -> bool:
    return start <= d <= end


def _bucket(pos: float) -> str | None:
    if pos < 1:
        return None
    for name, hi in (("1-3", 3), ("4-6", 6), ("7-10", 10), ("11-20", 20)):
        if pos <= hi:
            return name
    return None


def _dr(inp: GrowthInput) -> dict[str, str]:
    return {"start": inp.current_start.isoformat(), "end": inp.current_end.isoformat()}


def _opp(inp: GrowthInput, *, key: str, type_: str, url: str | None, title: str,
         description: str, evidence: dict, sources: list[str], metric: str,
         value: float | None, unit: str, method: str, confidence: float, effort: str,
         action: str, validation: str, limitations: str) -> Opportunity:
    return Opportunity(
        key=key, opportunity_type=type_, url=url, title=title, description=description,
        evidence=evidence, data_sources=sources, date_range=_dr(inp),
        impact_estimate={"metric": metric, "value": None if value is None else round(value, 2),
                         "unit": unit, "method": method},
        confidence=round(max(0.0, min(1.0, confidence)), 2), effort=effort,
        priority_score=0.0, recommended_action=action, validation_metric=validation,
        limitations=limitations,
    )


def _gsc_current(inp: GrowthInput) -> list[GscRow]:
    return [r for r in inp.gsc_rows if _in(r.date, inp.current_start, inp.current_end)]


def _page_clicks(rows: list[GscRow]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out[normalize_path(r.page)] += r.clicks
    return out


def _ga4_pages(inp: GrowthInput) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in inp.ga4_rows:
        if not _in(r.date, inp.current_start, inp.current_end):
            continue
        p = out.setdefault(normalize_path(r.landing_page),
                           {"sessions": 0, "conversions": 0, "tracked": 0})
        p["sessions"] += r.sessions
        if r.signups is not None or r.purchases is not None:
            p["tracked"] = 1
        p["conversions"] += (r.signups or 0) + (r.purchases or 0)
    return out


def _agg_page_query(rows: list[GscRow]) -> dict[tuple[str, str], dict[str, float]]:
    agg: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        a = agg.setdefault((normalize_path(r.page), r.query),
                           {"clicks": 0, "impressions": 0, "pos_w": 0.0})
        a["clicks"] += r.clicks
        a["impressions"] += r.impressions
        a["pos_w"] += r.position * r.impressions
    for a in agg.values():
        a["position"] = a["pos_w"] / a["impressions"] if a["impressions"] else 0.0
    return agg


def _low_ctr(inp: GrowthInput, t: Thresholds) -> list[Opportunity]:
    agg = _agg_page_query(_gsc_current(inp))
    by_bucket: dict[str, list[float]] = defaultdict(list)
    for a in agg.values():
        b = _bucket(a["position"])
        if b and a["impressions"] > 0 and a["position"] <= t.max_position:
            by_bucket[b].append(a["clicks"] / a["impressions"])
    out = []
    for (page, query), a in sorted(agg.items()):
        b = _bucket(a["position"])
        if not b or a["position"] > t.max_position or a["impressions"] < t.min_impressions:
            continue
        ctrs = by_bucket[b]
        if len(ctrs) < t.min_bucket_rows:
            continue
        expected = median(ctrs)
        ctr = a["clicks"] / a["impressions"]
        if ctr >= t.low_ctr_ratio * expected:
            continue
        extra = (expected - ctr) * a["impressions"]
        conf = 0.5 + min(0.3, a["impressions"] / 10000) + (0.1 if len(ctrs) >= 20 else 0.0)
        out.append(_opp(
            inp, key=f"low_ctr|{page}|{query}", type_="low_ctr", url=page,
            title=f"Low CTR for '{query}'",
            description=f"CTR {ctr:.2%} is below half of the site's median {expected:.2%} "
                        f"for position bucket {b}.",
            evidence={"query": query, "impressions": int(a["impressions"]),
                      "clicks": int(a["clicks"]), "ctr": round(ctr, 4),
                      "avg_position": round(a["position"], 2), "position_bucket": b,
                      "expected_ctr": round(expected, 4), "bucket_rows": len(ctrs)},
            sources=["gsc"], metric="extra_clicks", value=extra, unit="clicks/period",
            method="(expected_ctr - actual_ctr) * impressions, where expected_ctr is the "
                   "median CTR of this site's own (page, query) rows in the same position bucket.",
            confidence=conf, effort="low",
            action="Rewrite title/meta description to better match the query intent and test.",
            validation="CTR for this page+query in GSC next period",
            limitations="Expected CTR derives only from this site's own data; SERP features "
                        "and brand queries can distort CTR."))
    return out


def _declining(inp: GrowthInput, t: Thresholds) -> list[Opportunity]:
    cur = _page_clicks(_gsc_current(inp))
    prev = _page_clicks([r for r in inp.gsc_rows
                         if _in(r.date, inp.previous_start, inp.previous_end)])
    ys, ye = inp.current_start - timedelta(days=365), inp.current_end - timedelta(days=365)
    yoy = _page_clicks([r for r in inp.gsc_rows if _in(r.date, ys, ye)])
    out = []
    for page in sorted(prev):
        p, c = prev[page], cur.get(page, 0)
        if p < t.decline_min_previous_clicks:
            continue
        drop = (p - c) / p
        if drop < t.decline_pct:
            continue
        conf = 0.5 if p < 100 else 0.65 if p < 500 else 0.8
        ev = {"clicks_current": c, "clicks_previous": p, "drop_pct": round(drop * 100, 1)}
        if page in yoy:
            ev["clicks_year_ago"] = yoy[page]
            season = "Year-ago clicks included in evidence to judge seasonality."
        else:
            ev["clicks_year_ago"] = None
            season = "Seasonality unknown: no data from 365 days earlier."
        out.append(_opp(
            inp, key=f"declining_performance|{page}", type_="declining_performance", url=page,
            title=f"Clicks down {drop:.0%}",
            description=f"Clicks fell from {p} to {c} versus the previous equal-length period.",
            evidence=ev, sources=["gsc"], metric="lost_clicks", value=float(p - c),
            unit="clicks/period", method="previous_period_clicks - current_period_clicks.",
            confidence=conf, effort="medium",
            action="Investigate query/position changes, content freshness, and technical issues.",
            validation="Page clicks in GSC vs previous period",
            limitations=season + " Small volumes lower confidence."))
    return out


def _indexing(inp: GrowthInput, t: Thresholds) -> list[Opportunity]:
    issues: dict[str, set[str]] = defaultdict(set)
    for i in inp.issues:
        if i.rule_id in INDEXING_RULES:
            issues[normalize_path(i.url)].add(i.rule_id)
    clicks = _page_clicks(_gsc_current(inp)) if inp.gsc_available else {}
    ga4 = _ga4_pages(inp) if inp.ga4_available else {} if inp.ga4_available else {}
    out = []
    for page in sorted(issues):
        c = clicks.get(page, 0)
        g = ga4.get(page, {"sessions": 0, "conversions": 0})
        if not (c >= t.indexing_min_clicks or g["sessions"] >= t.indexing_min_sessions
                or g["conversions"] > 0):
            continue
        sources = [s for s, ok in (("gsc", inp.gsc_available), ("ga4", inp.ga4_available),
                                   ("crawl", True)) if ok]
        use_clicks = inp.gsc_available
        out.append(_opp(
            inp, key=f"indexing_issue_on_valuable_page|{page}",
            type_="indexing_issue_on_valuable_page", url=page,
            title="Indexing/accessibility issue on a page with traffic",
            description="Page has issues that may block indexing: "
                        + ", ".join(sorted(issues[page])) + ".",
            evidence={"rule_ids": sorted(issues[page]), "gsc_clicks": c if use_clicks else None,
                      "ga4_sessions": g["sessions"] if inp.ga4_available else None,
                      "conversions": g["conversions"] if inp.ga4_available else None},
            sources=sources, metric="traffic_at_risk",
            value=float(c if use_clicks else g["sessions"]),
            unit="clicks/period" if use_clicks else "sessions/period",
            method="Current-period organic clicks (GSC) or sessions (GA4) of the affected page.",
            confidence=0.8, effort="low",
            action="Fix the flagged issue(s) and request re-crawl/indexing.",
            validation="Issue absent in next crawl; page impressions stable in GSC",
            limitations="Traffic at risk is an upper bound; the issue may not currently cause loss."))
    return out


def _low_conversion(inp: GrowthInput, t: Thresholds) -> list[Opportunity]:
    pages = _ga4_pages(inp)
    if not any(p["tracked"] for p in pages.values()):
        return []
    big = {k: v for k, v in pages.items() if v["sessions"] >= t.min_sessions}
    if len(big) < t.min_pages_for_median:
        return []
    rates = {k: v["conversions"] / v["sessions"] for k, v in big.items()}
    med = median(rates.values())
    if med <= 0:
        return []
    out = []
    for page in sorted(big):
        rate = rates[page]
        if rate >= t.low_conversion_ratio * med:
            continue
        s = big[page]["sessions"]
        out.append(_opp(
            inp, key=f"low_conversion|{page}", type_="low_conversion", url=page,
            title="Landing page converts below site median",
            description=f"Conversion rate {rate:.2%} vs site median {med:.2%}.",
            evidence={"sessions": s, "conversions": big[page]["conversions"],
                      "conversion_rate": round(rate, 4), "median_rate": round(med, 4),
                      "pages_compared": len(big)},
            sources=["ga4"], metric="extra_conversions", value=s * (med - rate),
            unit="conversions/period", method="sessions * (median_rate - page_rate).",
            confidence=0.5 + min(0.3, s / 5000), effort="medium",
            action="Review landing page intent match, CTA, and form friction; run an A/B test.",
            validation="Conversion rate for this landing page in GA4",
            limitations="Conversions = signups + purchases; traffic mix by channel is not controlled."))
    return out


def _linking(inp: GrowthInput, t: Thresholds) -> list[Opportunity]:
    g = inp.graph
    if g is None:
        return []
    inl: dict[str, int] = {}
    dep: dict[str, int] = {}
    for u, v in g.inlinks.items():
        inl[normalize_path(u)] = v
    for u, v in g.depth.items():
        dep[normalize_path(u)] = v
    clicks = _page_clicks(_gsc_current(inp)) if inp.gsc_available else {}
    ga4 = _ga4_pages(inp) if inp.ga4_available else {}
    out = []
    for page in sorted(set(inl) | set(dep)):
        c = clicks.get(page, 0)
        conv = ga4.get(page, {}).get("conversions", 0)
        if not (c >= t.linking_min_clicks or conv > 0):
            continue
        n, d = inl.get(page), dep.get(page)
        weak = (n is not None and n <= t.max_inlinks) or (d is not None and d >= t.min_depth)
        if not weak:
            continue
        out.append(_opp(
            inp, key=f"weak_internal_linking|{page}", type_="weak_internal_linking", url=page,
            title="Valuable page is weakly linked internally",
            description=f"Inlinks: {n}, click depth: {d}.",
            evidence={"inlinks": n, "depth": d, "gsc_clicks": c if inp.gsc_available else None,
                      "conversions": conv if inp.ga4_available else None},
            sources=[s for s, ok in (("crawl", True), ("gsc", inp.gsc_available),
                                     ("ga4", inp.ga4_available)) if ok],
            metric="unknown", value=None, unit="n/a",
            method="No quantitative estimate: the effect of added links is not predictable "
                   "from first-party data.",
            confidence=0.6, effort="low",
            action="Add contextual internal links from relevant, well-linked pages and reduce depth.",
            validation="Inlink count/depth in next crawl; page impressions in GSC",
            limitations="Link graph covers crawled pages only; impact is not quantified."))
    return out


def _content_intent(inp: GrowthInput, t: Thresholds) -> list[Opportunity]:
    agg = _agg_page_query(_gsc_current(inp))
    by_q: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for (page, q), a in agg.items():
        by_q[q][page] = a
    out = []
    for q in sorted(by_q):
        pg = by_q[q]
        total = sum(a["impressions"] for a in pg.values())
        if total < t.min_impressions:
            continue
        shares = {p: a["impressions"] / total for p, a in pg.items()}
        split = sorted(p for p, s in shares.items() if s >= t.cannibal_share)
        pages_ev = [{"page": p, "impressions": int(pg[p]["impressions"]),
                     "clicks": int(pg[p]["clicks"]), "position": round(pg[p]["position"], 2),
                     "impression_share": round(shares[p], 3)} for p in sorted(pg)]
        if len(split) >= 2:
            out.append(_opp(
                inp, key=f"content_intent|cannibalisation|{q}", type_="content_intent",
                url=None, title=f"Possible cannibalisation for '{q}'",
                description=f"{len(split)} pages each hold >= {t.cannibal_share:.0%} of impressions.",
                evidence={"subtype": "cannibalisation", "query": q,
                          "total_impressions": int(total), "pages": pages_ev},
                sources=["gsc"], metric="unknown", value=None, unit="n/a",
                method="Not quantified: no volume or difficulty data is invented.",
                confidence=0.55, effort="medium",
                action="Pick a primary page; consolidate or differentiate intent and link to it.",
                validation="Single page ranking for the query; combined clicks in GSC",
                limitations="Splitting may be legitimate (different intents/SERP features)."))
            continue
        pos = sum(a["position"] * a["impressions"] for a in pg.values()) / total
        if (t.striking_min_position <= pos <= t.striking_max_position
                and total >= t.striking_min_impressions):
            top = max(sorted(pg), key=lambda p: pg[p]["impressions"])
            out.append(_opp(
                inp, key=f"content_intent|{top}|{q}", type_="content_intent", url=top,
                title=f"Striking distance query '{q}'",
                description=f"Average position {pos:.1f} with {int(total)} impressions.",
                evidence={"subtype": "striking_distance", "query": q,
                          "total_impressions": int(total), "avg_position": round(pos, 2),
                          "pages": pages_ev},
                sources=["gsc"], metric="unknown", value=None, unit="n/a",
                method="Not quantified: no volume or difficulty data is invented.",
                confidence=0.5, effort="medium",
                action="Strengthen content depth and intent match; add internal links for the query.",
                validation="Average position and clicks for the query in GSC",
                limitations="Position is an impression-weighted average; ranking gains not guaranteed."))
    return out


def _prioritize(opps: list[Opportunity]) -> list[Opportunity]:
    vals = [o.impact_estimate["value"] for o in opps if o.impact_estimate["value"] is not None]
    denom = math.log10(1 + max(vals)) if vals else 0.0
    for o in opps:
        v = o.impact_estimate["value"]
        if v is None:
            norm = 0.3
        else:
            norm = math.log10(1 + max(v, 0.0)) / denom if denom > 0 else 0.0
        o.priority_score = round(norm * o.confidence * EFFORT_FACTOR[o.effort] * 100, 2)
    return sorted(opps, key=lambda o: (-o.priority_score, o.key))


def generate_opportunities(inp: GrowthInput, thresholds: Thresholds = Thresholds()) -> list[Opportunity]:
    """Generate prioritized opportunities; skips types whose data sources are unavailable."""
    opps: list[Opportunity] = []
    if inp.gsc_available:
        opps += _low_ctr(inp, thresholds)
        opps += _declining(inp, thresholds)
        opps += _content_intent(inp, thresholds)
    opps += _indexing(inp, thresholds)
    if inp.ga4_available:
        opps += _low_conversion(inp, thresholds)
    opps += _linking(inp, thresholds)
    return _prioritize(opps)
