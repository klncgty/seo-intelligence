"""Read-side metric queries. None means "no data" — never reported as 0.

GSC clicks/impressions come from SearchTotalsDaily (unanonymised site totals); GA4 sessions/users
come from OrganicLandingDaily filtered to the "Organic Search" channel. They are different
measurements and are never merged. GA4 users are summed over daily/landing rows, so they are
not de-duplicated users (documented limitation).
"""

from datetime import date

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import OrganicLandingDaily as GA, SearchPerformanceDaily as SP, SearchTotalsDaily as ST
from app.services.growth_service import ORGANIC_CHANNEL


def _nsum(col):
    """SUM that stays NULL when every row is NULL (e.g. events not configured)."""
    return func.sum(col)


def gsc_totals(db: Session, site_id: int, start: date, end: date) -> dict | None:
    clicks, impr, wpos, n = db.execute(
        select(func.sum(ST.clicks), func.sum(ST.impressions), func.sum(ST.position * ST.impressions), func.count())
        .where(ST.site_id == site_id, ST.date.between(start, end))).one()
    if not n:
        return None
    return {"clicks": int(clicks), "impressions": int(impr), "ctr": (clicks / impr) if impr else None,
            "position": (wpos / impr) if impr else None}


def ga4_totals(db: Session, site_id: int, start: date, end: date) -> dict | None:
    r = db.execute(select(func.count(), func.sum(GA.sessions), func.sum(GA.total_users), _nsum(GA.signups),
                          _nsum(GA.purchases), _nsum(GA.revenue), _nsum(GA.key_events))
                   .where(GA.site_id == site_id, GA.channel_group == ORGANIC_CHANNEL, GA.date.between(start, end))).one()
    if not r[0]:
        return None
    return {"sessions": int(r[1]), "users": int(r[2]), "signups": r[3], "purchases": r[4],
            "revenue": float(r[5]) if r[5] is not None else None, "key_events": r[6]}


def site_metrics(db: Session, site_id: int, start: date, end: date) -> dict:
    g, a = gsc_totals(db, site_id, start, end) or {}, ga4_totals(db, site_id, start, end) or {}
    return {"clicks": g.get("clicks"), "impressions": g.get("impressions"), "ctr": g.get("ctr"), "position": g.get("position"),
            "sessions": a.get("sessions"), "users": a.get("users"), "signups": a.get("signups"),
            "purchases": a.get("purchases"), "revenue": a.get("revenue")}


def change_pct(cur: dict, prev: dict) -> dict:
    out = {}
    for k in ("clicks", "impressions", "sessions", "signups", "purchases", "revenue"):
        c, p = cur.get(k), prev.get(k)
        out[k] = round((c - p) / p * 100, 2) if c is not None and p not in (None, 0) else None
    return out


def sum_metrics(items: list[dict]) -> dict:
    out = {}
    for k in ("clicks", "impressions", "sessions", "users", "signups", "purchases", "revenue"):
        vals = [m[k] for m in items if m.get(k) is not None]
        out[k] = sum(vals) if vals else None
    out["ctr"] = out["clicks"] / out["impressions"] if out.get("impressions") else None
    out["position"] = None  # not meaningful across sites
    return out


def search_performance(db: Session, site_id: int, start: date, end: date, dimension: str, offset: int, limit: int) -> dict:
    totals = gsc_totals(db, site_id, start, end)
    if dimension in ("date", "device"):
        col = ST.date if dimension == "date" else ST.device
        model_filter = (ST.site_id == site_id, ST.date.between(start, end))
        q = (select(col.label("key"), func.sum(ST.clicks), func.sum(ST.impressions),
                    func.sum(ST.position * ST.impressions)).where(*model_filter).group_by(col))
        q = q.order_by(col) if dimension == "date" else q.order_by(desc(func.sum(ST.clicks)))
        note = None
    else:
        col = {"page": SP.page, "query": SP.query, "country": SP.country}[dimension]
        q = (select(col.label("key"), func.sum(SP.clicks), func.sum(SP.impressions),
                    func.sum(SP.position * SP.impressions))
             .where(SP.site_id == site_id, SP.date.between(start, end)).group_by(col)
             .order_by(desc(func.sum(SP.clicks)), col))
        note = "Breakdown from query-level rows; anonymized queries are excluded, so sums are below site totals."
    total_rows = db.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    rows = [{"key": str(k), "clicks": int(c), "impressions": int(i), "ctr": (c / i) if i else None,
             "position": (w / i) if i else None} for k, c, i, w in db.execute(q.offset(offset).limit(limit))]
    return {"has_data": totals is not None, "start": start.isoformat(), "end": end.isoformat(), "totals": totals,
            "rows": rows, "total_rows": total_rows, "note": note}


def landing_pages(db: Session, site_id: int, start: date, end: date, sort: str, offset: int, limit: int) -> dict:
    conv = func.coalesce(func.sum(GA.signups), 0) + func.coalesce(func.sum(GA.purchases), 0)
    base = (select(GA.landing_page, func.sum(GA.sessions).label("sessions"), func.sum(GA.total_users),
                   func.sum(GA.active_users), func.sum(GA.key_events), func.sum(GA.signups), func.sum(GA.purchases),
                   func.sum(GA.revenue).label("revenue"))
            .where(GA.site_id == site_id, GA.channel_group == ORGANIC_CHANNEL, GA.date.between(start, end))
            .group_by(GA.landing_page))
    order = {"sessions": desc("sessions"), "revenue": desc(func.coalesce(func.sum(GA.revenue), 0)), "conversions": desc(conv)}[sort]
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    items = []
    for lp, s, u, au, ke, su, pu, rev in db.execute(base.order_by(order, GA.landing_page).offset(offset).limit(limit)):
        conversions = None if su is None and pu is None else (su or 0) + (pu or 0)
        items.append({"landing_page": lp, "sessions": int(s), "total_users": int(u), "active_users": au,
                      "key_events": ke, "signups": su, "purchases": pu, "revenue": float(rev) if rev is not None else None,
                      "conversion_rate": (conversions / s) if conversions is not None and s else None})
    return {"has_data": total > 0, "start": start.isoformat(), "end": end.isoformat(), "items": items, "total": total}


def conversions(db: Session, site_id: int, events: dict, start: date, end: date) -> dict:
    totals = ga4_totals(db, site_id, start, end)
    daily = [{"date": d.isoformat(), "sessions": int(s), "signups": su, "purchases": pu,
              "revenue": float(r) if r is not None else None}
             for d, s, su, pu, r in db.execute(
                 select(GA.date, func.sum(GA.sessions), func.sum(GA.signups), func.sum(GA.purchases), func.sum(GA.revenue))
                 .where(GA.site_id == site_id, GA.channel_group == ORGANIC_CHANNEL, GA.date.between(start, end))
                 .group_by(GA.date).order_by(GA.date))]
    if totals:
        totals = {"sessions": totals["sessions"], "total_users": totals["users"], "signups": totals["signups"],
                  "purchases": totals["purchases"], "revenue": totals["revenue"], "key_events": totals["key_events"]}
    return {"has_data": totals is not None, "start": start.isoformat(), "end": end.isoformat(),
            "configured_events": events, "totals": totals, "daily": daily}

