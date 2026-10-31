"""SERP & rank tracking (spec §6).

Flow (job DAG): rank_check (fetch SERPs via provider runtime → global serp_results → per-tenant
rank_tracking_results) → rank_alerts (evaluate rules on the new check).

Formulas (documented in docs/metrics.md / docs/models/ctr-curve.md):
- CTR curve `ctr-curve-1` (static, desktop/mobile same): see CTR_CURVE. Calibration with own GSC is Phase 6.
- visibility = Σ CTR(pos_k) / (N · CTR(1)) · 100 over N tracked keywords (not ranking → 0).
- estimated traffic (MODELED) = volume · CTR(pos); null when volume unknown.
- share of voice = est. traffic of domain / Σ est. traffic of all tracked domains.
- volatility (per locale/device, day d vs previous snapshot): mean over keywords of
  min(10, Σ_url |rank_d − rank_prev| / 10) with missing URL rank = 11 → 0–10 scale.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.db.session import SessionLocal
from app.infra.clickhouse import ClickHouse
from app.models import RankAlert, RankProject, TrackedKeyword
from app.normalize.domains import registrable_domain
from app.normalize.locale import SearchContext
from app.services import credits
from app.services.keywords import service as kw_service
from app.services.keywords import store

CTR_MODEL_VERSION = "ctr-curve-1"
CTR_CURVE = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.018}
VOLATILITY_HIGH = 5.0


def ctr(pos: int | None) -> float:
    if pos is None or pos < 1:
        return 0.0
    return CTR_CURVE.get(pos, 0.01 if pos <= 20 else 0.0)


def visibility(positions: list[int | None]) -> float | None:
    if not positions:
        return None
    return round(sum(ctr(p) for p in positions) / (len(positions) * ctr(1)) * 100, 2)


def checks_per_run(p: RankProject, n_keywords: int) -> int:
    return n_keywords * max(1, len(p.devices or [])) * max(1, len(p.locations or [""]))


def monthly_estimate(p: RankProject, n_keywords: int) -> dict:
    runs = 30 if p.frequency == "daily" else 4
    qty = checks_per_run(p, n_keywords) * runs
    return {"serp_requests_per_month": qty, "estimated_credits_per_month": credits.estimate("serp_request", qty)}


def compute_positions(serp_organic: list[dict], domains: list[str]) -> dict[str, dict]:
    """Best organic position + landing URL per tracked registrable domain (None = not in fetched depth)."""
    out = {d: {"position": None, "landing_url": ""} for d in domains}
    for r in serp_organic:
        d = registrable_domain(r.get("domain") or "") or r.get("domain")
        if d in out and out[d]["position"] is None:
            out[d] = {"position": int(r["rank_group"]), "landing_url": r.get("url") or ""}
    return out


async def run_check(project_id: int, check_date: date | None = None, ch: ClickHouse | None = None) -> dict:
    """Fetch SERPs for every tracked keyword × device × location and store per-domain positions."""
    ch = ch or ClickHouse()
    check_date = check_date or date.today()
    with SessionLocal() as db:
        p = db.get(RankProject, project_id)
        kws = db.scalars(select(TrackedKeyword).where(TrackedKeyword.rank_project_id == p.id).order_by(TrackedKeyword.id)).all()
        if not p.budget_confirmed:
            raise PermissionError("recurring credit estimate not confirmed for this rank project")
        domains = [p.own_domain] + list(p.competitor_domains or [])
        rows, version = [], store._version()
        for device in p.devices or ["desktop"]:
            for location in p.locations or [""]:
                ctx = SearchContext(p.database, device, location)
                await kw_service.fetch_serps(db, ch, p.org_id, ctx, [k.keyword for k in kws], True,
                                             f"rank:{p.id}:{check_date}:{device}:{location}")
                for k in kws:
                    serp = store.latest_serp(ch, k.keyword_key, p.database, device, location)
                    if not serp["available"]:
                        continue
                    pos = compute_positions(serp["organic"], domains)
                    owned = [r["result_type"] for r in serp["all"] if r["result_type"] != "organic"
                             and (registrable_domain(r.get("domain") or "") or r.get("domain")) == p.own_domain]
                    for d, v in pos.items():
                        rows.append({"tenant_id": p.org_id, "project_id": p.id, "tracked_keyword_id": k.id,
                                     "keyword_key": k.keyword_key, "locale": p.database, "device": device, "location": location,
                                     "check_date": check_date.isoformat(), "domain": d, "position": v["position"],
                                     "landing_url": v["landing_url"], "serp_features_owned": owned if d == p.own_domain else [],
                                     "source_class": serp["source_class"], "provider": serp["provider"],
                                     "fetched_at": str(serp["fetched_at"]), "is_synthetic": int(serp["is_synthetic"]),
                                     "version": version})
        ch.insert("rank_tracking_results", rows)
        p.last_check_date = check_date
        db.commit()
    return {"rows": len(rows), "check_date": check_date.isoformat()}


def history(ch: ClickHouse, org_id: int, project_id: int, tracked_keyword_id: int | None = None) -> list[dict]:
    extra = " AND tracked_keyword_id = {kid:UInt64}" if tracked_keyword_id else ""
    return ch.tenant_query(
        "SELECT check_date, tracked_keyword_id, keyword_key, device, location, domain, position, landing_url, serp_features_owned, "
        "source_class, provider, is_synthetic FROM rank_tracking_results FINAL WHERE tenant_id = {tenant_id:UInt64} "
        "AND project_id = {pid:UInt64}" + extra + " ORDER BY check_date, tracked_keyword_id, domain",  # nosec B608 - fixed fragment
        org_id, {"pid": project_id, **({"kid": tracked_keyword_id} if tracked_keyword_id else {})})


def overview(db: Session, ch: ClickHouse, p: RankProject) -> dict:
    from app.services.domains import make_provenance

    rows = history(ch, p.org_id, p.id)
    if not rows:
        return {"available": False, "reason": "no rank checks yet"}
    vols = {r["keyword_key"]: r.get("volume") for r in store.explorer(ch, p.database, limit=10000)["items"]} if rows else {}
    by_date: dict[str, dict[str, list]] = {}
    for r in rows:
        pos = int(r["position"]) if r["position"] not in (None, "\\N") else None
        by_date.setdefault(str(r["check_date"]), {}).setdefault(r["domain"], []).append((r["keyword_key"], pos, r))
    dates = sorted(by_date)
    series = []
    for d in dates:
        entry = {"date": d, "visibility": {}, "estimated_traffic": {}, "distribution": {}}
        for dom, items in by_date[d].items():
            positions = [x[1] for x in items]
            ranked_pos = [x for x in positions if x is not None and x > 0]
            entry["visibility"][dom] = visibility(positions)
            traffic = [int(vols[k]) * ctr(pos) for k, pos, _ in items if vols.get(k) is not None]
            entry["estimated_traffic"][dom] = round(sum(traffic), 1) if traffic else None
            entry["distribution"][dom] = {
                "top3": sum(1 for x in positions if x and x <= 3),
                "top10": sum(1 for x in positions if x and x <= 10),
                "top20": sum(1 for x in positions if x and x <= 20),
                "top100": sum(1 for x in positions if x and x <= 100),
                "avg_position": round(sum(ranked_pos) / len(ranked_pos), 2) if ranked_pos else None,
            }
        total = sum(v for v in entry["estimated_traffic"].values() if v)
        entry["share_of_voice"] = {k: (round(v / total, 4) if v and total else None) for k, v in entry["estimated_traffic"].items()}
        series.append(entry)

    won, lost = [], []
    new_ranks, lost_ranks, improved_ranks, declined_ranks = [], [], [], []
    if len(dates) >= 2:
        prev = {k: pos for k, pos, _ in by_date[dates[-2]].get(p.own_domain, [])}
        for k, pos, _ in by_date[dates[-1]].get(p.own_domain, []):
            was = prev.get(k)
            if pos and pos <= 10 and not (was and was <= 10):
                won.append({"keyword_key": k, "from": was, "to": pos})
            if was and was <= 10 and not (pos and pos <= 10):
                lost.append({"keyword_key": k, "from": was, "to": pos})
            if pos and not was:
                new_ranks.append({"keyword_key": k, "from": None, "to": pos})
            elif was and not pos:
                lost_ranks.append({"keyword_key": k, "from": was, "to": None})
            elif pos and was:
                if pos < was:
                    improved_ranks.append({"keyword_key": k, "from": was, "to": pos, "delta": was - pos})
                elif pos > was:
                    declined_ranks.append({"keyword_key": k, "from": was, "to": pos, "delta": was - pos})

    latest_own = by_date[dates[-1]].get(p.own_domain, []) if dates else []
    feat_counts: dict[str, int] = {}
    for _, _, r in latest_own:
        for f in r.get("serp_features_owned") or []:
            feat_counts[f] = feat_counts.get(f, 0) + 1

    src_cls = rows[-1]["source_class"]
    is_synth = bool(rows[-1].get("is_synthetic"))
    return {
        "available": True,
        "domains": [p.own_domain] + list(p.competitor_domains or []),
        "series": series,
        "won_top10": won,
        "lost_top10": lost,
        "ranking_changes": {
            "new": new_ranks,
            "lost": lost_ranks,
            "improved": improved_ranks,
            "declined": declined_ranks,
            "counts": {
                "new": len(new_ranks),
                "lost": len(lost_ranks),
                "improved": len(improved_ranks),
                "declined": len(declined_ranks),
            },
        },
        "serp_features_summary": {
            "owned_counts": feat_counts,
            "featured_snippet_count": feat_counts.get("featured_snippet", 0),
        },
        "ctr_model": CTR_MODEL_VERSION,
        "source_classes": {"positions": src_cls, "visibility": "MODELED", "estimated_traffic": "MODELED"},
        "provenance": make_provenance(
            rows[-1].get("provider") or "SERP_Analyzer",
            src_cls,
            str(rows[-1].get("fetched_at") or dates[-1]),
            str(rows[-1].get("fetched_at") or dates[-1]),
            is_synth,
            0.9 if not is_synth else 0.6,
        ),
        "note": "Tracked rank (SERP snapshot) is not GSC average position; see /gsc-comparison.",
    }


def keyword_rankings_table(
    db: Session,
    ch: ClickHouse,
    p: RankProject,
    device: str | None = None,
    location: str | None = None,
) -> dict:
    """Comprehensive per-keyword ranking matrix for Modül 3 (§5):
    - Current & previous position, 1d / 7d / 30d deltas
    - Historical best & worst positions
    - Target / observed landing URL
    - Volume & estimated traffic contribution
    - Competitor positions side-by-side
    - SERP features & Featured Snippet ownership
    - Mobile vs Desktop rank comparison
    """
    from app.services.domains import make_provenance

    kws = db.scalars(select(TrackedKeyword).where(TrackedKeyword.rank_project_id == p.id).order_by(TrackedKeyword.id)).all()
    rows = history(ch, p.org_id, p.id)
    vols = {r["keyword_key"]: r.get("volume") for r in store.explorer(ch, p.database, limit=10000)["items"]} if kws else {}

    by_kid: dict[int, list[dict]] = {}
    for r in rows:
        by_kid.setdefault(int(r["tracked_keyword_id"]), []).append(r)

    items = []
    for kw in kws:
        k_rows = by_kid.get(kw.id, [])
        own_rows = [
            r for r in k_rows
            if r["domain"] == p.own_domain
            and (device is None or r["device"] == device)
            and (location is None or r["location"] == location)
        ]
        own_by_date: dict[str, dict] = {}
        for r in own_rows:
            d_str = str(r["check_date"])
            pos_val = int(r["position"]) if r["position"] not in (None, "\\N") else None
            existing = own_by_date.get(d_str)
            if existing is None or (pos_val is not None and (existing["pos"] is None or pos_val < existing["pos"])):
                own_by_date[d_str] = {"pos": pos_val, "row": r}

        sorted_dates = sorted(own_by_date)
        cur_date = sorted_dates[-1] if sorted_dates else None
        prev_date = sorted_dates[-2] if len(sorted_dates) >= 2 else None
        cur_entry = own_by_date.get(cur_date) if cur_date else None
        prev_entry = own_by_date.get(prev_date) if prev_date else None

        cur_pos = cur_entry["pos"] if cur_entry else None
        prev_pos = prev_entry["pos"] if prev_entry else None

        def _pos_near_offset(days_back: int) -> int | None:
            if not cur_date:
                return None
            target = (date.fromisoformat(cur_date) - __import__("datetime").timedelta(days=days_back)).isoformat()
            candidates = [d for d in sorted_dates if d <= target]
            if candidates:
                return own_by_date[candidates[-1]]["pos"]
            return prev_pos if len(sorted_dates) >= 2 else None

        pos_1d = prev_pos
        pos_7d = _pos_near_offset(7)
        pos_30d = _pos_near_offset(30)

        delta_1d = (pos_1d - cur_pos) if (cur_pos is not None and pos_1d is not None) else None
        delta_7d = (pos_7d - cur_pos) if (cur_pos is not None and pos_7d is not None) else None
        delta_30d = (pos_30d - cur_pos) if (cur_pos is not None and pos_30d is not None) else None

        all_ranked = [e["pos"] for e in own_by_date.values() if e["pos"] is not None]
        best_pos = min(all_ranked) if all_ranked else None
        worst_pos = max(all_ranked) if all_ranked else None

        # Mobile vs desktop on latest date (from rank_tracking_results + serp_results fallback)
        desktop_pos, mobile_pos = None, None
        comp_positions: dict[str, int | None] = {c: None for c in (p.competitor_domains or [])}
        if cur_date:
            for r in k_rows:
                if str(r["check_date"]) != cur_date:
                    continue
                rp_val = int(r["position"]) if r["position"] not in (None, "\\N") else None
                if r["domain"] == p.own_domain:
                    if r["device"] == "desktop" and rp_val is not None:
                        desktop_pos = rp_val
                    elif r["device"] == "mobile" and rp_val is not None:
                        mobile_pos = rp_val
                elif r["domain"] in comp_positions and (device is None or r["device"] == device):
                    if rp_val is not None and (comp_positions[r["domain"]] is None or rp_val < comp_positions[r["domain"]]):
                        comp_positions[r["domain"]] = rp_val
        if desktop_pos is None:
            s_desk = store.latest_serp(ch, kw.keyword_key, p.database, "desktop", "")
            if s_desk["available"]:
                desktop_pos = compute_positions(s_desk["organic"], [p.own_domain]).get(p.own_domain, {}).get("position")
        if mobile_pos is None:
            s_mob = store.latest_serp(ch, kw.keyword_key, p.database, "mobile", "")
            if s_mob["available"]:
                mobile_pos = compute_positions(s_mob["organic"], [p.own_domain]).get(p.own_domain, {}).get("position")

        vol = int(vols[kw.keyword_key]) if vols.get(kw.keyword_key) is not None else None
        est_traffic = round(vol * ctr(cur_pos), 2) if (vol is not None and cur_pos is not None) else None
        feats = list(cur_entry["row"].get("serp_features_owned") or []) if cur_entry else []
        landing_url = (cur_entry["row"].get("landing_url") if cur_entry else None) or kw.target_url

        if cur_pos is not None and prev_pos is None and len(sorted_dates) >= 2:
            status = "new"
        elif cur_pos is None and prev_pos is not None:
            status = "lost"
        elif cur_pos is not None and prev_pos is not None:
            status = "improved" if cur_pos < prev_pos else ("declined" if cur_pos > prev_pos else "unchanged")
        else:
            status = "unchanged"

        raw_row = cur_entry["row"] if cur_entry else None
        items.append({
            "id": kw.id,
            "keyword": kw.keyword,
            "keyword_key": kw.keyword_key,
            "tags": kw.tags or [],
            "important": kw.important,
            "target_url": kw.target_url,
            "landing_url": landing_url,
            "current_position": cur_pos,
            "previous_position": prev_pos,
            "delta_1d": delta_1d,
            "delta_7d": delta_7d,
            "delta_30d": delta_30d,
            "best_position": best_pos,
            "worst_position": worst_pos,
            "volume": vol,
            "estimated_traffic": est_traffic,
            "competitor_positions": comp_positions,
            "serp_features_owned": feats,
            "featured_snippet_owned": "featured_snippet" in feats,
            "desktop_position": desktop_pos,
            "mobile_position": mobile_pos,
            "mobile_vs_desktop_diff": (desktop_pos - mobile_pos) if (desktop_pos is not None and mobile_pos is not None) else None,
            "device": raw_row["device"] if raw_row else (device or (p.devices or ["desktop"])[0]),
            "location": raw_row["location"] if raw_row else (location or (p.locations or [""])[0]),
            "database": p.database,
            "status": status,
            "provenance": make_provenance(
                raw_row.get("provider") if raw_row else "SERP_Analyzer",
                raw_row.get("source_class", "PROVIDER") if raw_row else "PROVIDER",
                str(raw_row.get("fetched_at")) if raw_row else None,
                str(raw_row.get("fetched_at")) if raw_row else None,
                bool(raw_row and raw_row.get("is_synthetic")),
                0.9 if raw_row and not raw_row.get("is_synthetic") else 0.6,
            ),
        })

    return {
        "project_id": p.id,
        "own_domain": p.own_domain,
        "competitor_domains": list(p.competitor_domains or []),
        "database": p.database,
        "total": len(items),
        "items": items,
    }


def volatility(ch: ClickHouse, database: str, device: str = "desktop", days: int = 30, db: Session | None = None) -> dict:
    rows = ch.query(
        "SELECT snapshot_date, keyword_key, url, min(rank_group) AS r FROM serp_results FINAL WHERE locale = {db:String} "
        "AND device = {dev:String} AND result_type = 'organic' AND rank_group <= 10 "
        "AND snapshot_date >= today() - {days:UInt32} GROUP BY snapshot_date, keyword_key, url", {"db": database, "dev": device, "days": days})
    snaps: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows:
        snaps.setdefault(r["keyword_key"], {}).setdefault(str(r["snapshot_date"]), {})[r["url"]] = int(r["r"])
    per_day: dict[str, list[float]] = {}
    for kw, by_day in snaps.items():
        days_sorted = sorted(by_day)
        for a, b in zip(days_sorted, days_sorted[1:]):
            urls = set(by_day[a]) | set(by_day[b])
            delta = sum(abs(by_day[b].get(u, 11) - by_day[a].get(u, 11)) for u in urls)
            per_day.setdefault(b, []).append(min(10.0, delta / 10))
    annotations_by_day: dict[str, list[str]] = {}
    if db is not None and per_day:
        from datetime import timedelta
        from app.models import CalendarEvent
        s_day = date.fromisoformat(min(per_day))
        e_day = date.fromisoformat(max(per_day))
        evs = db.scalars(select(CalendarEvent).where(CalendarEvent.start_date <= e_day, CalendarEvent.end_date >= s_day)).all()
        for d_str in per_day:
            d_obj = date.fromisoformat(d_str)
            annotations_by_day[d_str] = [e.name for e in evs if e.start_date <= d_obj <= e.end_date]
    series = [{"date": d, "score": round(sum(v) / len(v), 2), "keywords": len(v),
               "label": "high fluctuation observed" if sum(v) / len(v) >= VOLATILITY_HIGH else "normal",
               "annotations": annotations_by_day.get(d, [])}
              for d, v in sorted(per_day.items())]
    return {"database": database, "device": device, "series": series, "universe": "tracked keywords with ≥2 snapshots",
            "note": "Observed SERP movement in our tracked universe; not a claim that a Google update happened."}


async def geo_grid_check(db: Session, ch: ClickHouse, org_id: int, keyword: str, own_domain: str, database: str,
                         center_lat: float, center_lng: float, grid_size: int = 3, step_km: float = 2.0,
                         device: str = "mobile", confirmed: bool = False, request_id: str = "geo") -> dict:
    """Geo-grid rank map (§6): queries a grid_size × grid_size coordinate grid with mandatory preflight credit confirmation."""
    import math
    if grid_size not in (3, 5, 7):
        raise ValueError("grid_size must be 3, 5, or 7")
    n_points = grid_size * grid_size
    kw_service._charge_or_preflight(db, org_id, "serp_request", n_points, confirmed, f"geogrid:{request_id}")
    d_dom = registrable_domain(own_domain) or own_domain
    lang = database.split("-")[0]
    from app.normalize.text import match_key, normalize_keyword
    norm_kw = normalize_keyword(keyword, lang)
    kw_key = match_key(norm_kw, lang)
    half = grid_size // 2
    lat_deg_per_km = 1.0 / 111.0
    lng_deg_per_km = 1.0 / max(1e-6, 111.0 * math.cos(math.radians(center_lat)))

    points = []
    for r_idx in range(-half, half + 1):
        for c_idx in range(-half, half + 1):
            lat = round(center_lat + r_idx * step_km * lat_deg_per_km, 5)
            lng = round(center_lng + c_idx * step_km * lng_deg_per_km, 5)
            loc = f"{lat},{lng}"
            ctx = SearchContext(database, device, loc)
            await kw_service.fetch_serps(db, ch, org_id, ctx, [norm_kw], True, f"{request_id}:{r_idx}:{c_idx}")
            serp = store.latest_serp(ch, kw_key, database, device, loc)
            pos = compute_positions(serp["organic"], [d_dom]).get(d_dom, {}) if serp["available"] else {}
            points.append({
                "row": r_idx + half,
                "col": c_idx + half,
                "lat": lat,
                "lng": lng,
                "location": loc,
                "position": pos.get("position"),
                "landing_url": pos.get("landing_url") or None,
            })
    ranked = [p["position"] for p in points if p["position"] is not None]
    return {
        "keyword": norm_kw,
        "own_domain": d_dom,
        "database": database,
        "device": device,
        "grid_size": grid_size,
        "step_km": step_km,
        "points": points,
        "avg_rank": round(sum(ranked) / len(ranked), 1) if ranked else None,
        "top3_share": round(sum(1 for r in ranked if r <= 3) / len(points), 3),
        "source_class": "PROVIDER",
    }


def evaluate_alerts(db: Session, ch: ClickHouse, p: RankProject, drop_threshold: int = 5) -> list[RankAlert]:
    rows = history(ch, p.org_id, p.id)
    dates = sorted({str(r["check_date"]) for r in rows})
    if len(dates) < 2:
        return []
    cur, prev = dates[-1], dates[-2]
    idx = {(str(r["check_date"]), r["tracked_keyword_id"], r["domain"], r["device"], r["location"]): r for r in rows}
    kws = {k.id: k for k in db.scalars(select(TrackedKeyword).where(TrackedKeyword.rank_project_id == p.id))}
    created = []

    def add(kind, kw, severity, details, key):
        dk = f"{p.id}:{kind}:{key}:{cur}"
        if db.scalar(select(RankAlert.id).where(RankAlert.dedupe_key == dk)):
            return
        a = RankAlert(org_id=p.org_id, rank_project_id=p.id, alert_type=kind, keyword=kw.keyword, severity=severity,
                      details=details, dedupe_key=dk)
        db.add(a)
        created.append(a)

    def pos(r):
        return int(r["position"]) if r and r["position"] not in (None, "\\N") else None

    for (d, kid, dom, dev, loc), r in idx.items():
        if d != cur or kid not in kws:
            continue
        before = idx.get((prev, kid, dom, dev, loc))
        now_p, was_p = pos(r), pos(before)
        kw = kws[kid]
        if dom == p.own_domain:
            dropped_5plus = bool(kw.important and was_p and (now_p is None or now_p - was_p >= drop_threshold))
            if dropped_5plus:
                add("rank_drop", kw, "high", {"from": was_p, "to": now_p, "device": dev, "location": loc}, f"{kid}:{dev}:{loc}")
            if now_p and now_p <= 3 and not (was_p and was_p <= 3):
                add("top3_entry", kw, "low", {"from": was_p, "to": now_p, "device": dev, "location": loc}, f"{kid}:{dev}:{loc}")
            elif now_p and now_p <= 10 and not (was_p and was_p <= 10):
                add("top10_entry", kw, "low", {"from": was_p, "to": now_p, "device": dev, "location": loc}, f"{kid}:{dev}:{loc}")
            if len(dates) >= 3:
                prev2 = idx.get((dates[-3], kid, dom, dev, loc))
                was2_p = pos(prev2)
                if was2_p and was_p and now_p and was2_p < was_p < now_p:
                    add("sustained_decline", kw, "high", {"positions": [was2_p, was_p, now_p], "device": dev, "location": loc}, f"{kid}:{dev}:{loc}")
            for comp in p.competitor_domains or []:
                c_now = pos(idx.get((cur, kid, comp, dev, loc)))
                c_was = pos(idx.get((prev, kid, comp, dev, loc)))
                if was_p and now_p and c_was and c_now:
                    if was_p > c_was and now_p < c_now:
                        add("competitor_overtake", kw, "low", {"competitor": comp, "own_from": was_p, "own_to": now_p, "comp_to": c_now}, f"{kid}:{comp}:{dev}:{loc}")
                    elif was_p < c_was and now_p > c_now and not dropped_5plus:
                        add("falling_behind_competitor", kw, "medium", {"competitor": comp, "own_from": was_p, "own_to": now_p, "comp_to": c_now}, f"{kid}:{comp}:{dev}:{loc}")
            if before and r["landing_url"] and before["landing_url"] and r["landing_url"] != before["landing_url"]:
                add("landing_url_change", kw, "medium", {"from": before["landing_url"], "to": r["landing_url"]}, f"{kid}:{dev}:{loc}")
            lost_features = set(before["serp_features_owned"] if before else []) - set(r["serp_features_owned"])
            if lost_features:
                add("serp_feature_lost", kw, "medium", {"features": sorted(lost_features)}, f"{kid}:{dev}:{loc}")
        elif now_p and now_p <= 3 and not (was_p and was_p <= 3):
            add("competitor_top3", kw, "medium", {"competitor": dom, "from": was_p, "to": now_p}, f"{kid}:{dom}:{dev}:{loc}")
    db.commit()
    return created


@jobs.handler("rank_check")
async def _rank_check(ctx: jobs.JobContext, payload: dict) -> dict:
    res = await run_check(payload["project_id"], date.fromisoformat(payload["check_date"]) if payload.get("check_date") else None)
    await ctx.heartbeat(progress=res)
    return res


@jobs.handler("rank_alerts")
async def _rank_alerts(ctx: jobs.JobContext, payload: dict) -> dict:
    with SessionLocal() as db:
        p = db.get(RankProject, payload["project_id"])
        created = evaluate_alerts(db, ClickHouse(), p)
        return {"alerts": len(created)}


def enqueue_check(db: Session, p: RankProject, check_date: date | None = None) -> dict:
    d = (check_date or date.today()).isoformat()
    check = jobs.enqueue(db, "rank_check", {"project_id": p.id, "check_date": d}, tenant_id=p.org_id,
                         idempotency_key=f"rank_check:{p.id}:{d}")
    alerts = jobs.enqueue(db, "rank_alerts", {"project_id": p.id}, tenant_id=p.org_id, depends_on=[check.id],
                          idempotency_key=f"rank_alerts:{p.id}:{d}")
    return {"check_job_id": check.id, "alerts_job_id": alerts.id}


def ensure_schedule(db: Session, p: RankProject) -> None:
    jobs.upsert_schedule(db, f"rank_project:{p.id}", "rank_dag", 86400 if p.frequency == "daily" else 7 * 86400,
                         {"project_id": p.id}, tenant_id=p.org_id)


@jobs.handler("rank_dag")
async def _rank_dag(ctx: jobs.JobContext, payload: dict) -> dict:
    with SessionLocal() as db:
        p = db.get(RankProject, payload["project_id"])
        if p is None or not p.is_active:
            return {"skipped": True}
        return enqueue_check(db, p)

