"""Growth Opportunity Engine v2 (spec §15) — orchestration, uncertainty, lifecycle, measurement, portfolio.

- regenerate(): runs every evidence source available for the site (GSC/GA4 v1 engine, competitors, backlink reclamation),
  then applies lifecycle rules: opportunities whose evidence disappeared get `resolution_suggested_at`; implemented ones
  whose evidence reappears ≥ 28 days after implementation are reopened (regression).
- Source-class confidence cap: confidence ≤ weight of the weakest source class in data_sources
  (FIRST_PARTY 1.0 > PROVIDER 0.85 > MODELED 0.7 > OBSERVED 0.6 > AI 0.4).
- Uncertainty: low_ctr impact P10/P50/P90 by bootstrapping daily impressions (500 resamples).
- Measurement loop: difference-in-differences for implemented page-level opportunities: treated = page clicks,
  control = site-total clicks minus the page, 28 days pre vs 28 days post (post window must be complete and final);
  95% bootstrap interval over days; "inconclusive" if the interval contains 0 or data are insufficient.
- Calibration: predicted P50 vs measured effect across measured opportunities.
"""

import random
import statistics
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import GrowthOpportunity, SearchPerformanceDaily as SP, SearchTotalsDaily as ST, Site, SplitTest
from app.services import growth_service

CLASS_WEIGHT = {"FIRST_PARTY": 1.0, "PROVIDER": 0.85, "MODELED": 0.7, "OBSERVED": 0.6, "AI": 0.4}
REGRESSION_AFTER = timedelta(days=28)
WINDOW = 28
EFFORT_HOURS = {"low": (1, 4), "medium": (4, 16), "high": (16, 60)}


def source_class_of(tag: str) -> str:
    t = tag.lower()
    if t.startswith(("gsc", "ga4", "bing")) or t == "gsc:own":
        return "FIRST_PARTY"
    if t.startswith(("provider", "backlinks:provider")):
        return "PROVIDER"
    if t.startswith("model"):
        return "MODELED"
    if t.startswith("ai"):
        return "AI"
    return "OBSERVED"  # crawl:, observed:, backlinks:own_crawl/common_crawl


def confidence_cap(data_sources: list[str]) -> tuple[float, str | None]:
    classes = [source_class_of(s) for s in data_sources or []]
    if not classes:
        return 1.0, None
    weakest = min(classes, key=lambda c: CLASS_WEIGHT[c])
    return CLASS_WEIGHT[weakest], weakest


def bootstrap_low_ctr(db: Session, o: GrowthOpportunity, n: int = 500, seed: int = 7) -> dict | None:
    ev = o.evidence or {}
    if o.opportunity_type != "low_ctr" or not o.date_range or "expected_ctr" not in ev:
        return None
    start, end = date.fromisoformat(o.date_range["start"]), date.fromisoformat(o.date_range["end"])
    from app.services.analytics_depth import canonical_path
    target = canonical_path(o.url or "")
    agg: dict = {}
    for d, page, impr, clicks in db.execute(select(SP.date, SP.page, func.sum(SP.impressions), func.sum(SP.clicks))
                                            .where(SP.site_id == o.site_id, SP.query == ev.get("query"), SP.date.between(start, end))
                                            .group_by(SP.date, SP.page)):
        if canonical_path(page) == target:  # match on canonical path (same join key as GSC↔GA4)
            a = agg.setdefault(d, [d, 0, 0])
            a[1] += impr
            a[2] += clicks
    rows = list(agg.values())
    if len(rows) < 7:
        return None
    rng = random.Random(seed)
    sims = []
    for _ in range(n):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        impr = sum(r[1] for r in sample)
        clicks = sum(r[2] for r in sample)
        sims.append(max(0.0, ev["expected_ctr"] * impr - clicks))
    sims.sort()
    return {"p10": round(sims[int(n * 0.1)], 1), "p50": round(sims[n // 2], 1), "p90": round(sims[int(n * 0.9)], 1),
            "method": "bootstrap over days (500 resamples) of (expected_ctr × impressions − clicks)"}


def regenerate(db: Session, site: Site, ch=None) -> dict:
    run_start = datetime.now(UTC)
    summary = {"gsc_ga4": growth_service.generate_for_site(db, site)}
    from app.services import competitor_service
    try:
        summary["competitors"] = competitor_service.generate_opportunities(db, site)
    except Exception as exc:  # optional source (needs crawls)
        summary["competitors"] = {"available": False, "reason": type(exc).__name__}
    if ch is not None and ch.configured:
        from app.services import backlinks
        try:
            summary["backlinks"] = backlinks.reclamation_opportunities(db, ch, site)
        except Exception as exc:
            summary["backlinks"] = {"available": False, "reason": type(exc).__name__}
    reopened = suggested = 0
    for o in db.scalars(select(GrowthOpportunity).where(GrowthOpportunity.site_id == site.id)):
        cap, weakest = confidence_cap(o.data_sources)
        if o.confidence > cap:
            o.confidence = round(cap, 3)
        o.evidence = {**(o.evidence or {}), "confidence_cap": {"value": cap, "weakest_source_class": weakest}}
        seen_now = o.last_seen_at is not None and o.last_seen_at >= run_start
        if seen_now:
            if o.status == "implemented" and o.implemented_at and run_start - o.implemented_at >= REGRESSION_AFTER:
                o.status, o.reopened_count = "new", (o.reopened_count or 0) + 1
                o.evidence = {**o.evidence, "reopened": {"at": run_start.isoformat(), "reason": "evidence reappeared after implementation"}}
                reopened += 1
            o.resolution_suggested_at = None
            boot = bootstrap_low_ctr(db, o)
            if boot:
                o.impact_estimate = {**(o.impact_estimate or {}), **boot}
        elif o.status in ("new", "planned", "in_progress") and o.resolution_suggested_at is None:
            o.resolution_suggested_at = run_start
            suggested += 1
    db.commit()
    return {**summary, "lifecycle": {"reopened": reopened, "resolution_suggested": suggested}}


def _daily(db: Session, site_id: int, page: str | None, start: date, end: date) -> dict[date, float]:
    if page is None:
        q = select(ST.date, func.sum(ST.clicks)).where(ST.site_id == site_id, ST.date.between(start, end)).group_by(ST.date)
    else:
        q = select(SP.date, func.sum(SP.clicks)).where(SP.site_id == site_id, SP.page == page, SP.date.between(start, end)).group_by(SP.date)
    return {d: float(v) for d, v in db.execute(q)}


def measure(db: Session, o: GrowthOpportunity, n_boot: int = 1000, seed: int = 11) -> dict:
    if o.status != "implemented" or not o.implemented_at or not o.url:
        return {"status": "not_applicable", "reason": "requires an implemented, page-level opportunity"}
    t0 = o.implemented_at.date()
    pre = (t0 - timedelta(days=WINDOW), t0 - timedelta(days=1))
    post = (t0 + timedelta(days=1), t0 + timedelta(days=WINDOW))
    if post[1] > date.today() - timedelta(days=3):
        return {"status": "pending", "reason": f"post window ends {post[1].isoformat()} (+3 days for final GSC data)"}
    tp, tq = _daily(db, o.site_id, o.url, *pre), _daily(db, o.site_id, o.url, *post)
    cp, cq = _daily(db, o.site_id, None, *pre), _daily(db, o.site_id, None, *post)
    if min(len(tp), len(tq), len(cp), len(cq)) < WINDOW * 0.75:
        result = {"status": "inconclusive", "reason": "insufficient daily data in pre/post windows"}
    else:
        def did(tpv, tqv, cpv, cqv):
            ctl_pre = [cpv[i] - tpv[i] for i in range(len(tpv))]
            ctl_post = [cqv[i] - tqv[i] for i in range(len(tqv))]
            return (statistics.mean(tqv) - statistics.mean(tpv)) - (statistics.mean(ctl_post) - statistics.mean(ctl_pre)) * \
                (statistics.mean(tpv) / max(statistics.mean(ctl_pre), 1e-9))
        days_pre = sorted(set(tp) & set(cp))
        days_post = sorted(set(tq) & set(cq))
        a = [tp[d] for d in days_pre]; b = [tq[d] for d in days_post]
        c = [cp[d] for d in days_pre]; e = [cq[d] for d in days_post]
        point = did(a, b, c, e)
        rng = random.Random(seed)
        sims = []
        for _ in range(n_boot):
            ip = [rng.randrange(len(a)) for _ in a]
            iq = [rng.randrange(len(b)) for _ in b]
            sims.append(did([a[i] for i in ip], [b[i] for i in iq], [c[i] for i in ip], [e[i] for i in iq]))
        sims.sort()
        lo, hi = sims[int(n_boot * 0.025)], sims[int(n_boot * 0.975) - 1]
        result = {"status": "inconclusive" if lo <= 0 <= hi else ("positive" if lo > 0 else "negative"),
                  "daily_clicks_effect": round(point, 2), "ci95": [round(lo, 2), round(hi, 2)],
                  "windows": {"pre": [pre[0].isoformat(), pre[1].isoformat()], "post": [post[0].isoformat(), post[1].isoformat()]}}
    result["method"] = "difference-in-differences (page vs rest of site, control scaled to page's pre level), bootstrap CI"
    o.measurement = {**result, "measured_at": datetime.now(UTC).isoformat()}
    db.commit()
    return result


def calibration(db: Session, site_id: int | None = None) -> dict:
    q = select(GrowthOpportunity).where(GrowthOpportunity.measurement.is_not(None))
    if site_id:
        q = q.where(GrowthOpportunity.site_id == site_id)
    rows = []
    for o in db.scalars(q):
        m = o.measurement or {}
        pred = (o.impact_estimate or {}).get("p50") or (o.impact_estimate or {}).get("value")
        if m.get("daily_clicks_effect") is None or pred is None:
            continue
        rows.append({"id": o.id, "type": o.opportunity_type, "predicted_p50_per_period": pred,
                     "measured_daily_effect": m["daily_clicks_effect"], "measured_period_effect": round(m["daily_clicks_effect"] * WINDOW, 1),
                     "status": m["status"]})
    hit = sum(1 for r in rows if r["status"] == "positive")
    return {"measured": len(rows), "positive": hit, "hit_rate": round(hit / len(rows), 2) if rows else None, "items": rows,
            "note": "Used to calibrate reach probabilities and impact priors; small samples are noisy."}


def portfolio(db: Session, weekly_hours: float = 20, weeks: int = 8) -> dict:
    """Cross-project prioritization + greedy weekly capacity plan + Kanban counts (RLS-scoped to the caller's org)."""
    opps = db.scalars(select(GrowthOpportunity).where(GrowthOpportunity.status.in_(("new", "planned", "in_progress")))
                      .order_by(GrowthOpportunity.priority_score.desc(), GrowthOpportunity.id)).all()
    plan, week, used = [[] for _ in range(weeks)], 0, 0.0
    for o in opps:
        lo, hi = EFFORT_HOURS.get(o.effort, (4, 16))
        need = (lo + hi) / 2
        while week < weeks and used + need > weekly_hours and plan[week]:
            week, used = week + 1, 0.0
        if week >= weeks:
            break
        plan[week].append({"id": o.id, "site_id": o.site_id, "title": o.title, "priority": o.priority_score, "hours": need})
        used += need
    kanban = dict(db.execute(select(GrowthOpportunity.status, func.count()).group_by(GrowthOpportunity.status)).all())
    return {"weekly_hours": weekly_hours, "plan": [{"week": i + 1, "items": p, "hours": sum(x["hours"] for x in p)} for i, p in enumerate(plan)],
            "kanban": kanban, "effort_hours": EFFORT_HOURS}


def stratified_split(db: Session, site_id: int, urls: list[str], lookback_days: int = 28) -> tuple[list[str], list[str]]:
    """Stratify candidate template URLs by historical GSC clicks into balanced control and variant buckets."""
    deduped = sorted(dict.fromkeys(u.strip() for u in urls if u.strip()))
    end = date.today() - timedelta(days=4)
    start = end - timedelta(days=lookback_days)
    clicks_by_page = dict(
        db.execute(
            select(SP.page, func.sum(SP.clicks))
            .where(SP.site_id == site_id, SP.page.in_(deduped), SP.date.between(start, end))
            .group_by(SP.page)
        ).all()
    ) if deduped else {}
    ranked = sorted(deduped, key=lambda u: (-int(clicks_by_page.get(u, 0)), u))
    control, variant = [], []
    for idx, u in enumerate(ranked):
        # Snake pairing (0->C, 1->V, 2->V, 3->C) balances top/bottom of each stratum pair
        pair = idx // 2
        to_control = (idx % 2 == 0) if (pair % 2 == 0) else (idx % 2 == 1)
        (control if to_control else variant).append(u)
    return control, variant


def create_split_test(db: Session, site: Site, name: str, hypothesis: str, urls: list[str],
                      template_sig: str | None = None, started_at: date | None = None) -> SplitTest:
    control, variant = stratified_split(db, site.id, urls)
    st = SplitTest(
        site_id=site.id,
        name=name,
        hypothesis=hypothesis,
        template_sig=template_sig,
        control_urls=control,
        variant_urls=variant,
        started_at=started_at or date.today(),
    )
    db.add(st)
    db.commit()
    return st


def evaluate_split_test(db: Session, st: SplitTest, n_boot: int = 500, seed: int = 17) -> dict:
    """Stratified template SEO split test evaluation via Difference-in-Differences + bootstrap CI."""
    if len(st.control_urls or []) < 1 or len(st.variant_urls or []) < 1 or not st.started_at:
        return {"status": "inconclusive", "reason": "requires non-empty control_urls, variant_urls, and started_at"}
    t0 = st.started_at
    pre = (t0 - timedelta(days=WINDOW), t0 - timedelta(days=1))
    post = (t0 + timedelta(days=1), t0 + timedelta(days=WINDOW))
    if post[1] > date.today() - timedelta(days=3):
        return {"status": "pending", "reason": f"post window ends {post[1].isoformat()} (+3 days for final GSC data)"}

    def _group_daily(urls: list[str], s: date, e: date) -> dict[date, float]:
        q = (
            select(SP.date, func.sum(SP.clicks))
            .where(SP.site_id == st.site_id, SP.page.in_(urls), SP.date.between(s, e))
            .group_by(SP.date)
        )
        return {d: float(v) for d, v in db.execute(q)}

    vp, vq = _group_daily(st.variant_urls, *pre), _group_daily(st.variant_urls, *post)
    cp, cq = _group_daily(st.control_urls, *pre), _group_daily(st.control_urls, *post)
    days_pre = sorted(set(vp) & set(cp))
    days_post = sorted(set(vq) & set(cq))
    if min(len(days_pre), len(days_post)) < int(WINDOW * 0.5):
        res = {"status": "inconclusive", "reason": "insufficient daily overlap in pre/post windows"}
    else:
        a = [vp[d] for d in days_pre]
        b = [vq[d] for d in days_post]
        c = [cp[d] for d in days_pre]
        e = [cq[d] for d in days_post]

        def did(av, bv, cv, ev):
            scale = statistics.mean(av) / max(statistics.mean(cv), 1e-9)
            return (statistics.mean(bv) - statistics.mean(av)) - (statistics.mean(ev) - statistics.mean(cv)) * scale

        point = did(a, b, c, e)
        rng = random.Random(seed)
        sims = []
        for _ in range(n_boot):
            ip = [rng.randrange(len(a)) for _ in a]
            iq = [rng.randrange(len(b)) for _ in b]
            sims.append(did([a[i] for i in ip], [b[i] for i in iq], [c[i] for i in ip], [e[i] for i in iq]))
        sims.sort()
        lo, hi = sims[int(n_boot * 0.025)], sims[int(n_boot * 0.975) - 1]
        res = {
            "status": "inconclusive" if lo <= 0 <= hi else ("positive" if lo > 0 else "negative"),
            "daily_clicks_effect": round(point, 2),
            "ci95": [round(lo, 2), round(hi, 2)],
            "windows": {"pre": [pre[0].isoformat(), pre[1].isoformat()], "post": [post[0].isoformat(), post[1].isoformat()]},
        }
    res["method"] = "stratified template split test (DiD variant vs control, bootstrap 95% CI)"
    st.result = {**res, "evaluated_at": datetime.now(UTC).isoformat()}
    db.commit()
    return st.result
