"""First-party analytics depth (spec §11).

Data-accuracy rules implemented here:
- GSC dates are Pacific Time; GA4 uses the property timezone → stored/flagged, comparisons carry a warning.
- The last GSC_PRELIMINARY_DAYS days are preliminary (dataState=all) → flagged and excluded from anomaly/forecast.
- Query coverage % = Σ clicks in query rows / site-total clicks (anonymized queries are missing from query rows).
- GSC↔GA4 join key: canonical path = lowercase path without trailing slash, query and fragment (documented below).
- Rates use Wilson score intervals (95%); cross-source "funnel" steps (GSC clicks → GA4 sessions) are labelled indicative.
- Anomalies: day-of-week baseline (median of the same weekday over the previous 8 weeks), robust z = (x − baseline) /
  (1.4826 · MAD of weekday residuals); |z| ≥ 3 and baseline ≥ MIN_VOLUME; calendar events annotate (not suppress) anomalies.
- Forecast (MODELED): only with ≥ 56 days of history; seasonal-naive (mean of last 4 same weekdays) with an interval from
  the empirical 10th/90th percentile of past one-step errors.
"""

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import CalendarEvent, OrganicLandingDaily as GA, SearchPerformanceDaily as SP, SearchTotalsDaily as ST, Site
from app.normalize.text import match_key
from app.services.growth_service import ORGANIC_CHANNEL

GSC_TZ = "America/Los_Angeles"
GSC_PRELIMINARY_DAYS = 3
MIN_VOLUME = 20
AI_REFERRERS = ("chatgpt.com", "chat.openai.com", "perplexity.ai", "gemini.google.com", "copilot.microsoft.com",
                "claude.ai", "you.com", "deepseek.com", "meta.ai")


def canonical_path(url_or_path: str) -> str:
    p = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?")[0].split("#")[0]
    p = (p or "/").lower()
    return p.rstrip("/") or "/"


def wilson(successes: int, n: int, z: float = 1.96) -> dict | None:
    if not n:
        return None
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return {"rate": round(phat, 5), "low": round(max(0.0, center - half), 5), "high": round(min(1.0, center + half), 5), "n": n}


def data_quality(db: Session, site: Site, start: date, end: date) -> dict:
    totals = db.scalar(select(func.sum(ST.clicks)).where(ST.site_id == site.id, ST.date.between(start, end)))
    detail = db.scalar(select(func.sum(SP.clicks)).where(SP.site_id == site.id, SP.date.between(start, end)))
    prelim_from = date.today() - timedelta(days=GSC_PRELIMINARY_DAYS)
    from app.services.google_service import integration_status
    ga4_meta = integration_status(db, site, "analytics").get("last_sync_metadata") or {}
    return {"ga4_data_quality": {"subject_to_thresholding": ga4_meta.get("subject_to_thresholding"),
                                 "data_loss_from_other_row": ga4_meta.get("data_loss_from_other_row"),
                                 "sampling": ga4_meta.get("sampling"), "property_quota": ga4_meta.get("property_quota")},
            "gsc_timezone": GSC_TZ, "ga4_timezone": site.ga4_timezone or "property timezone (not fetched)",
            "timezone_warning": "GSC (Pacific Time) and GA4 (property timezone) day boundaries differ; daily comparisons are approximate.",
            "preliminary_from": prelim_from.isoformat() if end >= prelim_from else None,
            "query_coverage_pct": round(detail / totals * 100, 1) if totals and detail is not None else None,
            "query_coverage_note": "Share of site-total clicks attributable to non-anonymized queries."}


def join_coverage(db: Session, site: Site, start: date, end: date) -> dict:
    gsc = dict(db.execute(select(SP.page, func.sum(SP.clicks)).where(SP.site_id == site.id, SP.date.between(start, end))
                          .group_by(SP.page)).all())
    ga = dict(db.execute(select(GA.landing_page, func.sum(GA.sessions)).where(GA.site_id == site.id, GA.channel_group == ORGANIC_CHANNEL,
                                                                             GA.date.between(start, end)).group_by(GA.landing_page)).all())
    gk, ak = defaultdict(int), defaultdict(int)
    for u, c in gsc.items():
        gk[canonical_path(u)] += int(c)
    for u, s in ga.items():
        ak[canonical_path(u)] += int(s)
    matched = set(gk) & set(ak)
    tg, ta = sum(gk.values()), sum(ak.values())
    return {"join_key": "lowercase URL path, no trailing slash/query/fragment",
            "gsc_clicks_matched_pct": round(sum(gk[k] for k in matched) / tg * 100, 1) if tg else None,
            "ga4_sessions_matched_pct": round(sum(ak[k] for k in matched) / ta * 100, 1) if ta else None,
            "unmatched_gsc_pages": sorted(set(gk) - matched, key=lambda k: -gk[k])[:50],
            "unmatched_ga4_landing_pages": sorted(set(ak) - matched, key=lambda k: -ak[k])[:50]}


def _group_of(path: str, groups: list[dict]) -> str:
    import regex
    for g in groups[:50]:
        try:
            if regex.search(g["pattern"], path, timeout=0.05):
                return g["name"]
        except (TimeoutError, regex.error):
            continue
    return "(other)"


def segments(db: Session, site: Site, start: date, end: date) -> dict:
    groups = list(site.page_groups or [])
    money = [{"name": "money", "pattern": p} for p in site.money_page_patterns or []]
    brand = [match_key(t) for t in site.brand_terms or [] if t]
    by_group = defaultdict(lambda: {"clicks": 0, "impressions": 0, "sessions": 0, "signups": None, "purchases": None, "revenue": None})
    for page, clicks, impr in db.execute(select(SP.page, func.sum(SP.clicks), func.sum(SP.impressions))
                                         .where(SP.site_id == site.id, SP.date.between(start, end)).group_by(SP.page)):
        g = by_group[_group_of(canonical_path(page), groups)]
        g["clicks"] += int(clicks)
        g["impressions"] += int(impr)
    for lp, s, su, pu, rev in db.execute(select(GA.landing_page, func.sum(GA.sessions), func.sum(GA.signups), func.sum(GA.purchases),
                                                func.sum(GA.revenue)).where(GA.site_id == site.id, GA.channel_group == ORGANIC_CHANNEL,
                                                                            GA.date.between(start, end)).group_by(GA.landing_page)):
        g = by_group[_group_of(canonical_path(lp), groups)]
        g["sessions"] += int(s)
        for k, v in (("signups", su), ("purchases", pu), ("revenue", rev)):
            if v is not None:
                g[k] = (g[k] or 0) + (float(v) if k == "revenue" else int(v))
    brand_split = None
    if brand:
        b = nb = 0
        for q, c in db.execute(select(SP.query, func.sum(SP.clicks)).where(SP.site_id == site.id, SP.date.between(start, end)).group_by(SP.query)):
            if any(t in match_key(q) for t in brand):
                b += int(c)
            else:
                nb += int(c)
        brand_split = {"brand_clicks": b, "non_brand_clicks": nb, "note": "query-level rows only (anonymized queries excluded)"}
    money_paths = None
    if money:
        mp = defaultdict(int)
        for page, c in db.execute(select(SP.page, func.sum(SP.clicks)).where(SP.site_id == site.id, SP.date.between(start, end)).group_by(SP.page)):
            if _group_of(canonical_path(page), money) == "money":
                mp["clicks"] += int(c)
        money_paths = dict(mp)
    funnel = {}
    for name, g in by_group.items():
        conv = None if g["signups"] is None and g["purchases"] is None else (g["signups"] or 0) + (g["purchases"] or 0)
        funnel[name] = {**g, "ctr": wilson(g["clicks"], g["impressions"]),
                        "conversion_rate": wilson(conv, g["sessions"]) if conv is not None else None,
                        "clicks_to_sessions_indicative": round(g["sessions"] / g["clicks"], 2) if g["clicks"] else None}
    ai = db.execute(select(GA.source_medium, func.sum(GA.sessions), func.sum(GA.signups), func.sum(GA.purchases))
                    .where(GA.site_id == site.id, GA.date.between(start, end),
                           or_(*[GA.source_medium.ilike(f"%{r}%") for r in AI_REFERRERS])).group_by(GA.source_medium)).all()
    return {"page_groups": funnel, "brand_split": brand_split, "money_pages": money_paths,
            "ai_referrals": {"sources_config": list(AI_REFERRERS), "items": [{"source_medium": sm, "sessions": int(s), "signups": su,
                                                                            "purchases": pu} for sm, s, su, pu in ai],
                             "source_class": "FIRST_PARTY"},
            "funnel_note": "impressions/clicks are GSC; sessions and conversions are GA4 — the clicks→sessions ratio is indicative only."}


def daily_series(db: Session, site: Site, metric: str, start: date, end: date) -> dict[date, float]:
    if metric == "gsc_clicks":
        q = select(ST.date, func.sum(ST.clicks)).where(ST.site_id == site.id, ST.date.between(start, end)).group_by(ST.date)
    elif metric == "ga4_sessions":
        q = (select(GA.date, func.sum(GA.sessions)).where(GA.site_id == site.id, GA.channel_group == ORGANIC_CHANNEL,
                                                          GA.date.between(start, end)).group_by(GA.date))
    else:
        raise ValueError("metric must be gsc_clicks or ga4_sessions")
    return {d: float(v) for d, v in db.execute(q)}


def events(db: Session, start: date, end: date, site_id: int | None = None) -> list[CalendarEvent]:
    return db.scalars(select(CalendarEvent).where(CalendarEvent.start_date <= end, CalendarEvent.end_date >= start,
                                                  or_(CalendarEvent.site_id.is_(None), CalendarEvent.site_id == site_id))
                      .order_by(CalendarEvent.start_date)).all()


def robust_decompose(series: dict[date, float]) -> dict[date, dict]:
    """STL-like robust decomposition (weekly period): trend = centered 7-day moving median, seasonal = median detrended
    value per weekday, residual = x − trend − seasonal."""
    days = sorted(series)
    out = {}
    trend = {}
    for d in days:
        win = [series[x] for x in (d + timedelta(days=k) for k in range(-3, 4)) if x in series]
        trend[d] = statistics.median(win)
    by_wd = defaultdict(list)
    for d in days:
        by_wd[d.weekday()].append(series[d] - trend[d])
    seasonal = {wd: statistics.median(v) for wd, v in by_wd.items()}
    for d in days:
        out[d] = {"trend": trend[d], "seasonal": seasonal[d.weekday()], "residual": series[d] - trend[d] - seasonal[d.weekday()]}
    return out


def anomalies(db: Session, site: Site, metric: str, days: int = 90, method: str = "weekday") -> dict:
    if method == "stl":
        return _anomalies_stl(db, site, metric, days)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days + 56)
    series = daily_series(db, site, metric, start, end)
    cutoff = date.today() - timedelta(days=GSC_PRELIMINARY_DAYS) if metric == "gsc_clicks" else end + timedelta(days=1)
    out = []
    evs = events(db, start, end, site.id)
    d = end - timedelta(days=days)
    while d <= end:
        if d >= cutoff or d not in series:
            d += timedelta(days=1)
            continue
        hist = [series.get(d - timedelta(weeks=w)) for w in range(1, 9)]
        hist = [h for h in hist if h is not None]
        if len(hist) >= 4:
            base = statistics.median(hist)
            mad = statistics.median(abs(h - base) for h in hist) or max(1.0, base * 0.05)
            z = (series[d] - base) / (1.4826 * mad)
            if abs(z) >= 3 and base >= MIN_VOLUME:
                tags = [e.name for e in evs if e.start_date <= d <= e.end_date]
                out.append({"date": d.isoformat(), "value": series[d], "baseline": base, "z": round(z, 2),
                            "direction": "drop" if z < 0 else "spike", "calendar_events": tags})
        d += timedelta(days=1)
    return {"metric": metric, "method": "weekday-median baseline + robust z (MAD), |z|≥3, min volume 20",
            "excluded_preliminary_from": cutoff.isoformat() if metric == "gsc_clicks" else None, "anomalies": out}


def forecast(db: Session, site: Site, metric: str, horizon: int = 14) -> dict:
    end = date.today() - timedelta(days=GSC_PRELIMINARY_DAYS + 1 if metric == "gsc_clicks" else 1)
    series = daily_series(db, site, metric, end - timedelta(days=180), end)
    if len(series) < 56:
        return {"available": False, "reason": "needs ≥ 56 days of history", "source_class": "MODELED"}
    errs = []
    days = sorted(series)
    for d in days[28:]:
        past = [series.get(d - timedelta(weeks=w)) for w in range(1, 5)]
        past = [p for p in past if p is not None]
        if len(past) == 4:
            errs.append(series[d] - sum(past) / 4)
    errs.sort()
    lo = errs[int(len(errs) * 0.1)] if errs else 0
    hi = errs[int(len(errs) * 0.9) - 1] if errs else 0
    pts = []
    ext = dict(series)
    for i in range(1, horizon + 1):
        d = end + timedelta(days=i)
        past = [ext.get(d - timedelta(weeks=w)) for w in range(1, 5)]
        past = [p for p in past if p is not None]
        yhat = sum(past) / len(past) if past else None
        ext[d] = yhat
        pts.append({"date": d.isoformat(), "yhat": round(yhat, 1) if yhat is not None else None,
                    "low": round(max(0.0, yhat + lo), 1) if yhat is not None else None,
                    "high": round(yhat + hi, 1) if yhat is not None else None})
    return {"available": True, "source_class": "MODELED", "model_version": "seasonal-naive-1", "metric": metric, "points": pts,
            "note": "Estimate with an 80% empirical interval; not a guarantee. Shown separately from actuals."}


def _anomalies_stl(db: Session, site: Site, metric: str, days: int) -> dict:
    end = date.today() - timedelta(days=1)
    cutoff = date.today() - timedelta(days=GSC_PRELIMINARY_DAYS) if metric == "gsc_clicks" else end + timedelta(days=1)
    series = {d: v for d, v in daily_series(db, site, metric, end - timedelta(days=days + 28), end).items() if d < cutoff}
    if len(series) < 28:
        return {"metric": metric, "method": "stl", "anomalies": [], "note": "needs ≥ 28 days"}
    dec = robust_decompose(series)
    res = [v["residual"] for v in dec.values()]
    med = statistics.median(res)
    mad = statistics.median(abs(r - med) for r in res) or 1.0
    evs = events(db, end - timedelta(days=days), end, site.id)
    out = []
    for d, v in sorted(dec.items()):
        if d < end - timedelta(days=days):
            continue
        z = (v["residual"] - med) / (1.4826 * mad)
        if abs(z) >= 3.5 and v["trend"] >= MIN_VOLUME:
            out.append({"date": d.isoformat(), "value": series[d], "expected": round(v["trend"] + v["seasonal"], 1), "z": round(z, 2),
                        "direction": "drop" if z < 0 else "spike",
                        "calendar_events": [e.name for e in evs if e.start_date <= d <= e.end_date]})
    return {"metric": metric, "method": "stl (robust weekly decomposition, |z|≥3.5 on residual MAD)", "anomalies": out}
