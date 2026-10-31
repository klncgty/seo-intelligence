"""Domain & competitor intelligence (spec §7, Module 1).

Sources are kept apart in every response:
- "observed": our own SERP snapshot universe (serp_results / domain_keyword_rankings) — OBSERVED/PROVIDER SERPs we fetched.
- "provider": provider ranked-keywords snapshots (domain_ranked_keywords) — PROVIDER.
- "actual": the tenant's own GSC/GA4 for its verified site — FIRST_PARTY, only for that tenant.
- estimated traffic & traffic value — MODELED (CTR curve × volume × CPC), with the model's typical error when calibrated.
"""

import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from lxml import etree
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infra.clickhouse import ClickHouse
from app.models import (
    Competitor,
    CrawlPage,
    CrawlRun,
    DomainAnalysisRecord,
    GrowthOpportunity,
    SearchPerformanceDaily,
    SearchTotalsDaily,
    Site,
)
from app.normalize.domains import registrable_domain
from app.normalize.locale import SearchContext
from app.normalize.text import match_key
from app.providers import registry, runtime
from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
from app.services.crawler.parser import maybe_gunzip, robots_sitemaps
from app.services.crawler.urls import host_of, normalize_url
from app.services.keywords import service as kw_service
from app.services.keywords import store
from app.services.rank_tracking import CTR_CURVE, ctr, visibility

CALIBRATION_MIN_IMPRESSIONS = 100


def confidence_label(score: float) -> str:
    if score >= 0.75:
        return "HIGH"
    if score >= 0.45:
        return "MEDIUM"
    return "LOW"


def make_provenance(
    data_source: str,
    source_class: str,
    collected_at: str | None = None,
    updated_at: str | None = None,
    is_estimated: bool = False,
    confidence_score: float = 0.7,
) -> dict:
    now_iso = datetime.now(UTC).isoformat()
    col = collected_at or now_iso
    upd = updated_at or col
    lvl = confidence_label(confidence_score)
    return {
        "data_source": data_source,
        "dataSource": data_source,
        "source_class": source_class,
        "collected_at": col,
        "collectedAt": col,
        "updated_at": upd,
        "updatedAt": upd,
        "is_estimated": is_estimated,
        "isEstimated": is_estimated,
        "confidence_score": round(confidence_score, 2),
        "confidenceScore": round(confidence_score, 2),
        "confidence_level": lvl,
        "confidenceLevel": lvl,
    }


def _volumes(ch: ClickHouse, database: str, keys: list[str]) -> dict[str, int | None]:
    if not keys:
        return {}
    rows = ch.query(
        "SELECT keyword_key, search_volume FROM keyword_latest FINAL WHERE locale = {db:String} "
        "AND has({keys:Array(String)}, keyword_key)",
        {"db": database, "keys": keys},
    )
    return {r["keyword_key"]: (int(r["search_volume"]) if r["search_volume"] is not None else None) for r in rows}


def _keyword_metadata(ch: ClickHouse, database: str, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    rows = ch.query(
        "SELECT keyword_key, keyword, search_volume, cpc, cpc_currency, kd, intents, fetched_at, provider, source_class "
        "FROM keyword_latest FINAL WHERE locale = {db:String} AND has({keys:Array(String)}, keyword_key)",
        {"db": database, "keys": list(set(keys))},
    )
    out: dict[str, dict] = {}
    for r in rows:
        out[r["keyword_key"]] = {
            "keyword": r.get("keyword") or r["keyword_key"],
            "search_volume": int(r["search_volume"]) if r.get("search_volume") is not None else None,
            "cpc": float(r["cpc"]) if r.get("cpc") is not None else None,
            "cpc_currency": r.get("cpc_currency") or "USD",
            "kd": round(float(r["kd"]), 1) if r.get("kd") is not None else None,
            "intents": list(r.get("intents") or []),
            "fetched_at": str(r.get("fetched_at")) if r.get("fetched_at") else None,
            "provider": r.get("provider") or "keyword_latest",
            "source_class": r.get("source_class") or "PROVIDER",
        }
    return out


def observed_rankings(ch: ClickHouse, domain: str, database: str, days: int = 30) -> list[dict]:
    return ch.query(
        "SELECT keyword_key, argMax(best_rank, snapshot_date) AS position, argMax(url, snapshot_date) AS url, "
        "max(snapshot_date) AS last_snapshot FROM domain_keyword_rankings FINAL WHERE registrable_domain = {d:String} "
        "AND locale = {db:String} AND snapshot_date >= today() - {days:UInt32} GROUP BY keyword_key ORDER BY position",
        {"d": domain, "db": database, "days": days},
    )


def provider_rankings(ch: ClickHouse, domain: str, database: str) -> list[dict]:
    return ch.query(
        "SELECT keyword, keyword_key, argMax(position, snapshot_date) AS position, argMax(url, snapshot_date) AS url, "
        "argMax(search_volume, snapshot_date) AS search_volume, max(snapshot_date) AS last_snapshot, any(provider) AS provider, "
        "max(is_synthetic) AS is_synthetic FROM domain_ranked_keywords FINAL WHERE registrable_domain = {d:String} "
        "AND locale = {db:String} GROUP BY keyword, keyword_key ORDER BY position",
        {"d": domain, "db": database},
    )


def _summary(
    rows: list[dict],
    vols: dict[str, int | None],
    curve: dict[int, float] | None = None,
    kw_meta: dict[str, dict] | None = None,
    panel_source: str = "SERP_Analyzer",
    panel_class: str = "OBSERVED",
) -> dict:
    kw_meta = kw_meta or {}
    positions = [int(r["position"]) for r in rows]
    last_snap = max((str(r["last_snapshot"]) for r in rows if r.get("last_snapshot")), default=None)

    est_list: list[float] = []
    val_list: list[float] = []
    page_traffic: dict[str, float] = defaultdict(float)
    page_value: dict[str, float] = defaultdict(float)
    page_kws: Counter[str] = Counter()
    page_best_pos: dict[str, int] = {}
    folder_kws: Counter[str] = Counter()
    folder_traffic: dict[str, float] = defaultdict(float)
    top_keywords: list[dict] = []

    for r in rows:
        pos = int(r["position"])
        kkey = r["keyword_key"]
        meta = kw_meta.get(kkey, {})
        vol = vols.get(kkey)
        if vol is None and r.get("search_volume") is not None:
            vol = int(r["search_volume"])
        cpc_val = meta.get("cpc")
        w = curve.get(pos, 0.0) if curve else ctr(pos)
        est_click = round(vol * w, 2) if vol is not None else None
        est_val = round(est_click * cpc_val, 2) if (est_click is not None and cpc_val is not None) else None
        if est_click is not None:
            est_list.append(est_click)
        if est_val is not None:
            val_list.append(est_val)

        u = r.get("url") or ""
        if u:
            page_kws[u] += 1
            if est_click is not None:
                page_traffic[u] += est_click
            if est_val is not None:
                page_value[u] += est_val
            if u not in page_best_pos or pos < page_best_pos[u]:
                page_best_pos[u] = pos
            folder = "/" + (u.split("/", 3)[3].split("/")[0] if u.count("/") >= 3 else "")
            folder_kws[folder] += 1
            if est_click is not None:
                folder_traffic[folder] += est_click

        top_keywords.append({
            "keyword": r.get("keyword") or meta.get("keyword") or kkey,
            "keyword_key": kkey,
            "position": pos,
            "url": u,
            "search_volume": vol,
            "cpc": cpc_val,
            "kd": meta.get("kd"),
            "intents": meta.get("intents") or [],
            "estimated_traffic": est_click,
            "estimated_traffic_value": est_val,
            "last_snapshot": str(r.get("last_snapshot")) if r.get("last_snapshot") else None,
        })

    top_keywords.sort(key=lambda x: (-(x["estimated_traffic"] or -1), x["position"], x["keyword_key"]))

    top_pages = []
    for u, n in page_kws.most_common(20):
        top_pages.append({
            "url": u,
            "keywords": n,
            "estimated_traffic": round(page_traffic[u], 1) if u in page_traffic else None,
            "estimated_traffic_value": round(page_value[u], 2) if u in page_value else None,
            "best_position": page_best_pos.get(u),
        })
    top_pages.sort(key=lambda x: (-(x["estimated_traffic"] or 0), -x["keywords"]))

    vis_score = visibility(positions)
    top3_n = sum(p <= 3 for p in positions)
    p4_10_n = sum(3 < p <= 10 for p in positions)
    p11_20_n = sum(10 < p <= 20 for p in positions)
    p21_100_n = sum(20 < p <= 100 for p in positions)

    return {
        "ranked_keywords": len(rows),
        "position_distribution": {
            "top3": top3_n,
            "1-3": top3_n,
            "4-10": p4_10_n,
            "11-20": p11_20_n,
            "21-100": p21_100_n,
            "top10": top3_n + p4_10_n,
            "top20": top3_n + p4_10_n + p11_20_n,
            "top100": top3_n + p4_10_n + p11_20_n + p21_100_n,
        },
        "visibility_score": {
            "value": vis_score,
            "weighted_ctr_sum": round(sum(ctr(p) for p in positions), 3) if positions else 0.0,
            **make_provenance("CTR_Visibility_Model_v1", "MODELED", last_snap, last_snap, True, 0.7 if positions else 0.3),
        },
        "estimated_traffic": {
            "value": round(sum(est_list), 1) if est_list else None,
            "source_class": "MODELED",
            "keywords_with_volume": len(est_list),
            "keywords_without_volume": len(rows) - len(est_list),
            **make_provenance("CTR_Traffic_Model_v1", "MODELED", last_snap, last_snap, True, 0.65 if est_list else 0.3),
        },
        "estimated_traffic_value": {
            "value": round(sum(val_list), 2) if val_list else None,
            "currency": "USD",
            "source_class": "MODELED",
            "keywords_with_cpc": len(val_list),
            **make_provenance("CTR_CPC_Valuation_v1", "MODELED", last_snap, last_snap, True, 0.6 if val_list else 0.3),
        },
        "top_pages": top_pages[:10],
        "top_keywords": top_keywords[:25],
        "subfolders": [
            {
                "folder": f,
                "keywords": n,
                "estimated_traffic": round(folder_traffic[f], 1) if f in folder_traffic else None,
            }
            for f, n in folder_kws.most_common(10)
        ],
        "provenance": make_provenance(panel_source, panel_class, last_snap, last_snap, False, 0.8 if rows else 0.4),
    }


def _backlink_summary_for_domain(ch: ClickHouse, domain: str) -> dict:
    try:
        rows = ch.query(
            "SELECT count() AS total_backlinks, uniqExact(source_domain) AS referring_domains, "
            "max(last_seen) AS last_seen, any(source_class) AS source_class, any(source) AS data_source "
            "FROM backlinks FINAL WHERE target_domain = {d:String} AND status = 'live'",
            {"d": domain},
        )
        ds_rows = ch.query(
            "SELECT argMax(score, version) AS score, max(computed_at) AS computed_at "
            "FROM domain_strength FINAL WHERE registrable_domain = {d:String}",
            {"d": domain},
        )
    except Exception:
        rows, ds_rows = [], []

    r = rows[0] if rows else {}
    total_bl = int(r.get("total_backlinks") or 0)
    ref_doms = int(r.get("referring_domains") or 0)
    last_seen = str(r["last_seen"]) if r.get("last_seen") and int(r.get("total_backlinks") or 0) > 0 else None
    src_class = r.get("source_class") or "OBSERVED"
    data_src = r.get("data_source") or "Internal_Backlink_Index"
    ds_score = round(float(ds_rows[0]["score"]), 1) if ds_rows and ds_rows[0].get("score") is not None and ref_doms > 0 else None

    return {
        "total_backlinks": total_bl if total_bl > 0 else None,
        "referring_domains": ref_doms if ref_doms > 0 else None,
        "domain_strength": ds_score,
        "last_seen": last_seen,
        **make_provenance(data_src, src_class, last_seen, last_seen, False, 0.8 if total_bl > 0 else 0.4),
    }


def _indexed_pages_summary(db: Session, ch: ClickHouse, domain: str, own_site: Site | None) -> dict:
    crawled_count = None
    last_crawl_at = None
    if own_site is not None:
        latest_run = db.scalar(
            select(CrawlRun)
            .where(CrawlRun.site_id == own_site.id, CrawlRun.competitor_id.is_(None), CrawlRun.status == "completed")
            .order_by(CrawlRun.id.desc())
            .limit(1)
        )
        if latest_run is not None:
            pages_200 = db.scalars(
                select(CrawlPage).where(
                    CrawlPage.crawl_run_id == latest_run.id,
                    CrawlPage.status_code == 200,
                    CrawlPage.is_html.is_(True),
                )
            ).all()
            crawled_count = sum(
                1
                for p in pages_200
                if "noindex" not in f"{p.meta_robots or ''} {p.x_robots_tag or ''}".lower()
            )
            last_crawl_at = latest_run.finished_at.isoformat() if latest_run.finished_at else None

    serp_urls = 0
    try:
        sr = ch.query(
            "SELECT uniqExact(url) AS u FROM serp_results WHERE registrable_domain = {d:String} AND result_type = 'organic'",
            {"d": domain},
        )
        serp_urls = int(sr[0]["u"]) if sr else 0
    except Exception:
        serp_urls = 0

    gsc_urls = None
    if own_site is not None:
        since = date.today() - timedelta(days=30)
        gsc_cnt = db.scalar(
            select(func.count(func.distinct(SearchPerformanceDaily.page))).where(
                SearchPerformanceDaily.site_id == own_site.id,
                SearchPerformanceDaily.date >= since,
            )
        )
        if gsc_cnt:
            gsc_urls = int(gsc_cnt)

    known_counts = [x for x in (crawled_count, serp_urls if serp_urls > 0 else None, gsc_urls) if x is not None]
    total_known = max(known_counts) if known_counts else None
    src_cls = "FIRST_PARTY" if gsc_urls is not None else ("OBSERVED" if total_known is not None else "OBSERVED")
    return {
        "total_known_urls": total_known,
        "crawled_indexable_pages": crawled_count,
        "serp_indexed_urls": serp_urls if serp_urls > 0 else None,
        "gsc_indexed_urls": gsc_urls,
        **make_provenance("Internal_Crawler_And_SERP", src_cls, last_crawl_at, last_crawl_at, False, 0.85 if total_known else 0.4),
    }


def _country_distribution(db: Session, ch: ClickHouse, domain: str, own_site: Site | None) -> dict:
    by_locale = []
    try:
        rows = ch.query(
            "SELECT locale, uniqExact(keyword_key) AS kws, max(snapshot_date) AS last_snapshot "
            "FROM domain_keyword_rankings FINAL WHERE registrable_domain = {d:String} "
            "GROUP BY locale ORDER BY kws DESC",
            {"d": domain},
        )
        total_kws = sum(int(r["kws"]) for r in rows) or 1
        for r in rows:
            loc = r["locale"]
            country_code = loc.split(":")[0].split("-")[-1].upper() if "-" in loc else loc[:2].upper()
            obs_rows = observed_rankings(ch, domain, loc, days=90)
            vols = _volumes(ch, loc, [x["keyword_key"] for x in obs_rows])
            est = sum(vols[x["keyword_key"]] * ctr(int(x["position"])) for x in obs_rows if vols.get(x["keyword_key"]) is not None)
            by_locale.append({
                "country": country_code,
                "locale": loc,
                "ranked_keywords": int(r["kws"]),
                "keyword_share_pct": round(int(r["kws"]) / total_kws * 100, 1),
                "estimated_traffic": round(est, 1) if est else None,
                "last_snapshot": str(r["last_snapshot"]) if r.get("last_snapshot") else None,
                "source_class": "OBSERVED",
            })
    except Exception:
        by_locale = []

    gsc_countries = []
    if own_site is not None:
        since = date.today() - timedelta(days=30)
        for country, clicks, impr, wpos in db.execute(
            select(
                SearchPerformanceDaily.country,
                func.sum(SearchPerformanceDaily.clicks),
                func.sum(SearchPerformanceDaily.impressions),
                func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions),
            )
            .where(SearchPerformanceDaily.site_id == own_site.id, SearchPerformanceDaily.date >= since)
            .group_by(SearchPerformanceDaily.country)
            .order_by(func.sum(SearchPerformanceDaily.clicks).desc())
        ):
            gsc_countries.append({
                "country": (country or "UNK").upper(),
                "clicks": int(clicks or 0),
                "impressions": int(impr or 0),
                "avg_position": round(float(wpos) / float(impr), 1) if impr else None,
                "source_class": "FIRST_PARTY",
            })

    return {
        "observed_by_country": by_locale,
        "first_party_gsc_countries": gsc_countries,
        **make_provenance("SERP_And_GSC_Geo", "FIRST_PARTY" if gsc_countries else "OBSERVED", None, None, not bool(gsc_countries), 0.8),
    }


def _device_distribution(db: Session, ch: ClickHouse, domain: str, database: str, vols: dict[str, int | None], own_site: Site | None) -> dict:
    obs_devices = []
    try:
        rows = ch.query(
            "SELECT device, keyword_key, argMax(best_rank, snapshot_date) AS position, max(snapshot_date) AS last_snapshot "
            "FROM domain_keyword_rankings FINAL WHERE registrable_domain = {d:String} AND locale = {db:String} "
            "GROUP BY device, keyword_key",
            {"d": domain, "db": database},
        )
        by_dev: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_dev[r.get("device") or "desktop"].append(r)
        for dev, drows in sorted(by_dev.items()):
            pos_list = [int(x["position"]) for x in drows]
            est = [vols[x["keyword_key"]] * ctr(int(x["position"])) for x in drows if vols.get(x["keyword_key"]) is not None]
            obs_devices.append({
                "device": dev,
                "ranked_keywords": len(drows),
                "estimated_traffic": round(sum(est), 1) if est else None,
                "visibility_score": visibility(pos_list),
                "top3": sum(p <= 3 for p in pos_list),
                "top10": sum(p <= 10 for p in pos_list),
                "source_class": "OBSERVED",
            })
    except Exception:
        obs_devices = []

    gsc_devices = []
    if own_site is not None:
        since = date.today() - timedelta(days=30)
        for dev, clicks, impr, wpos in db.execute(
            select(
                SearchPerformanceDaily.device,
                func.sum(SearchPerformanceDaily.clicks),
                func.sum(SearchPerformanceDaily.impressions),
                func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions),
            )
            .where(SearchPerformanceDaily.site_id == own_site.id, SearchPerformanceDaily.date >= since)
            .group_by(SearchPerformanceDaily.device)
            .order_by(func.sum(SearchPerformanceDaily.clicks).desc())
        ):
            c_int, i_int = int(clicks or 0), int(impr or 0)
            gsc_devices.append({
                "device": (dev or "DESKTOP").lower(),
                "clicks": c_int,
                "impressions": i_int,
                "ctr": round(c_int / i_int, 4) if i_int else 0.0,
                "avg_position": round(float(wpos) / i_int, 1) if i_int else None,
                "source_class": "FIRST_PARTY",
            })

    return {
        "observed_by_device": obs_devices,
        "first_party_gsc_devices": gsc_devices,
        **make_provenance("SERP_And_GSC_Devices", "FIRST_PARTY" if gsc_devices else "OBSERVED", None, None, not bool(gsc_devices), 0.8),
    }


def _visibility_and_trends(
    db: Session,
    ch: ClickHouse,
    domain: str,
    database: str,
    vols: dict[str, int | None],
    kw_meta: dict[str, dict],
    own_site: Site | None,
) -> tuple[dict, dict, dict]:
    """Compute historical visibility, ranking movements (new/lost/improved/declined), traffic trends, and organic growth."""
    by_date: dict[str, dict[str, dict]] = defaultdict(dict)
    try:
        rows = ch.query(
            "SELECT snapshot_date, keyword_key, min(best_rank) AS position, argMin(url, best_rank) AS url "
            "FROM domain_keyword_rankings FINAL WHERE registrable_domain = {d:String} AND locale = {db:String} "
            "AND snapshot_date >= today() - 90 GROUP BY snapshot_date, keyword_key ORDER BY snapshot_date ASC",
            {"d": domain, "db": database},
        )
        for r in rows:
            dt = str(r["snapshot_date"])
            by_date[dt][r["keyword_key"]] = {"position": int(r["position"]), "url": r.get("url") or ""}
    except Exception:
        by_date = defaultdict(dict)

    dates = sorted(by_date.keys())
    observed_series = []
    for dt in dates:
        snap = by_date[dt]
        pos_list = [v["position"] for v in snap.values()]
        est_t = 0.0
        est_v = 0.0
        has_vol = False
        for kkey, v in snap.items():
            vol = vols.get(kkey)
            if vol is not None:
                has_vol = True
                c = vol * ctr(v["position"])
                est_t += c
                cpc_val = kw_meta.get(kkey, {}).get("cpc")
                if cpc_val is not None:
                    est_v += c * cpc_val
        observed_series.append({
            "date": dt,
            "ranked_keywords": len(snap),
            "estimated_traffic": round(est_t, 1) if has_vol else None,
            "estimated_traffic_value": round(est_v, 2) if est_v > 0 else None,
            "visibility_score": visibility(pos_list),
            "top3": sum(p <= 3 for p in pos_list),
            "4-10": sum(3 < p <= 10 for p in pos_list),
            "11-20": sum(10 < p <= 20 for p in pos_list),
            "21-100": sum(20 < p <= 100 for p in pos_list),
        })

    # Ranking changes (new, lost, improved, declined) between latest and previous snapshot
    changes: dict[str, list[dict]] = {"new": [], "lost": [], "improved": [], "declined": []}
    if len(dates) >= 2:
        prev_dt, cur_dt = dates[-2], dates[-1]
        prev_map, cur_map = by_date[prev_dt], by_date[cur_dt]
        all_keys = sorted(set(prev_map) | set(cur_map))
        for kkey in all_keys:
            p_prev = prev_map.get(kkey, {}).get("position")
            p_cur = cur_map.get(kkey, {}).get("position")
            kw_label = kw_meta.get(kkey, {}).get("keyword") or kkey
            vol = vols.get(kkey)
            if p_prev is None and p_cur is not None:
                changes["new"].append({
                    "keyword": kw_label,
                    "keyword_key": kkey,
                    "current_position": p_cur,
                    "previous_position": None,
                    "delta": None,
                    "url": cur_map[kkey]["url"],
                    "search_volume": vol,
                })
            elif p_prev is not None and p_cur is None:
                changes["lost"].append({
                    "keyword": kw_label,
                    "keyword_key": kkey,
                    "current_position": None,
                    "previous_position": p_prev,
                    "delta": None,
                    "url": prev_map[kkey]["url"],
                    "search_volume": vol,
                })
            elif p_prev is not None and p_cur is not None:
                if p_cur < p_prev:
                    changes["improved"].append({
                        "keyword": kw_label,
                        "keyword_key": kkey,
                        "current_position": p_cur,
                        "previous_position": p_prev,
                        "delta": p_prev - p_cur,
                        "url": cur_map[kkey]["url"],
                        "search_volume": vol,
                    })
                elif p_cur > p_prev:
                    changes["declined"].append({
                        "keyword": kw_label,
                        "keyword_key": kkey,
                        "current_position": p_cur,
                        "previous_position": p_prev,
                        "delta": p_prev - p_cur,
                        "url": cur_map[kkey]["url"],
                        "search_volume": vol,
                    })
    elif len(dates) == 1:
        cur_dt = dates[0]
        for kkey, v in by_date[cur_dt].items():
            changes["new"].append({
                "keyword": kw_meta.get(kkey, {}).get("keyword") or kkey,
                "keyword_key": kkey,
                "current_position": v["position"],
                "previous_position": None,
                "delta": None,
                "url": v["url"],
                "search_volume": vols.get(kkey),
            })

    # First-party GSC daily series if own_site
    gsc_series = []
    gsc_growth_clicks_pct = None
    if own_site is not None:
        since_60 = date.today() - timedelta(days=60)
        daily_rows = db.execute(
            select(
                SearchTotalsDaily.date,
                func.sum(SearchTotalsDaily.clicks),
                func.sum(SearchTotalsDaily.impressions),
            )
            .where(SearchTotalsDaily.site_id == own_site.id, SearchTotalsDaily.date >= since_60)
            .group_by(SearchTotalsDaily.date)
            .order_by(SearchTotalsDaily.date.asc())
        ).all()
        if not daily_rows:
            daily_rows = db.execute(
                select(
                    SearchPerformanceDaily.date,
                    func.sum(SearchPerformanceDaily.clicks),
                    func.sum(SearchPerformanceDaily.impressions),
                )
                .where(SearchPerformanceDaily.site_id == own_site.id, SearchPerformanceDaily.date >= since_60)
                .group_by(SearchPerformanceDaily.date)
                .order_by(SearchPerformanceDaily.date.asc())
            ).all()
        for dt_val, c, i in daily_rows:
            gsc_series.append({
                "date": dt_val.isoformat(),
                "clicks": int(c or 0),
                "impressions": int(i or 0),
            })
        cutoff_30 = date.today() - timedelta(days=30)
        cur_30 = sum(x["clicks"] for x in gsc_series if x["date"] >= cutoff_30.isoformat())
        prev_30 = sum(x["clicks"] for x in gsc_series if x["date"] < cutoff_30.isoformat())
        if prev_30 > 0:
            gsc_growth_clicks_pct = round((cur_30 - prev_30) / prev_30 * 100, 1)

    kw_growth_pct = None
    traffic_growth_pct = None
    if len(observed_series) >= 2:
        prev_s, cur_s = observed_series[-2], observed_series[-1]
        if prev_s["ranked_keywords"] > 0:
            kw_growth_pct = round((cur_s["ranked_keywords"] - prev_s["ranked_keywords"]) / prev_s["ranked_keywords"] * 100, 1)
        if prev_s["estimated_traffic"] and cur_s["estimated_traffic"] is not None and prev_s["estimated_traffic"] > 0:
            traffic_growth_pct = round((cur_s["estimated_traffic"] - prev_s["estimated_traffic"]) / prev_s["estimated_traffic"] * 100, 1)

    cur_vis = observed_series[-1]["visibility_score"] if observed_series else None
    vis_payload = {
        "score_pct": cur_vis,
        "history": observed_series,
        "ranking_changes": {
            "counts": {k: len(v) for k, v in changes.items()},
            "items": {k: v[:50] for k, v in changes.items()},
        },
        **make_provenance("SERP_Visibility_Engine", "OBSERVED", dates[-1] if dates else None, dates[-1] if dates else None, True, 0.75 if dates else 0.35),
    }

    trends_payload = {
        "observed_series": observed_series,
        "first_party_gsc_series": gsc_series,
        **make_provenance("SERP_And_GSC_Time_Series", "FIRST_PARTY" if gsc_series else "MODELED", dates[-1] if dates else None, None, not bool(gsc_series), 0.75),
    }

    growth_payload = {
        "keywords_change_pct": kw_growth_pct,
        "estimated_traffic_change_pct": traffic_growth_pct,
        "gsc_clicks_change_pct": gsc_growth_clicks_pct,
        "snapshots_compared": len(observed_series),
        **make_provenance("Growth_Delta_Calculator", "MODELED", dates[-1] if dates else None, None, True, 0.7 if len(observed_series) >= 2 else 0.4),
    }

    return vis_payload, trends_payload, growth_payload


def overview(db: Session, ch: ClickHouse, domain: str, database: str, org_id: int) -> dict:
    d = registrable_domain(domain) or domain
    obs = observed_rankings(ch, d, database)
    prov = provider_rankings(ch, d, database)
    all_keys = list({r["keyword_key"] for r in obs + prov})
    vols = _volumes(ch, database, all_keys)
    kw_meta = _keyword_metadata(ch, database, all_keys)
    for k, m in kw_meta.items():
        if vols.get(k) is None and m.get("search_volume") is not None:
            vols[k] = m["search_volume"]

    # tenant's own verified site → "actual" panel (RLS: only sites of this org are visible)
    own_site = next((s for s in db.scalars(select(Site)) if (registrable_domain(s.base_url.split("/")[2]) or "") == d), None)
    actual = None
    if own_site:
        since = date.today() - timedelta(days=31)
        clicks, impr = db.execute(
            select(func.sum(SearchTotalsDaily.clicks), func.sum(SearchTotalsDaily.impressions)).where(
                SearchTotalsDaily.site_id == own_site.id, SearchTotalsDaily.date >= since
            )
        ).one()
        if clicks is None and impr is None:
            clicks, impr = db.execute(
                select(func.sum(SearchPerformanceDaily.clicks), func.sum(SearchPerformanceDaily.impressions)).where(
                    SearchPerformanceDaily.site_id == own_site.id, SearchPerformanceDaily.date >= since
                )
            ).one()
        cal = calibrate_ctr(db, own_site.id)
        actual = {
            "site_id": own_site.id,
            "gsc_clicks_30d": int(clicks) if clicks is not None else None,
            "gsc_impressions_30d": int(impr) if impr is not None else None,
            "source_class": "FIRST_PARTY",
            "calibration": cal,
            **make_provenance("Google_Search_Console", "FIRST_PARTY", None, None, False, 0.98 if clicks is not None else 0.5),
        }

    model_error = actual["calibration"].get("median_ape") if actual and actual["calibration"].get("available") else None
    obs_summary = _summary(obs, vols, kw_meta=kw_meta, panel_source="Internal_SERP_Snapshots", panel_class="OBSERVED")
    prov_summary = _summary(prov, vols, kw_meta=kw_meta, panel_source="DataForSEO_API", panel_class="PROVIDER")

    backlink_summary = _backlink_summary_for_domain(ch, d)
    indexed_pages = _indexed_pages_summary(db, ch, d, own_site)
    country_dist = _country_distribution(db, ch, d, own_site)
    device_dist = _device_distribution(db, ch, d, database, vols, own_site)
    vis_payload, trends_payload, growth_payload = _visibility_and_trends(db, ch, d, database, vols, kw_meta, own_site)

    result = {
        "domain": d,
        "database": database,
        "observed": {
            **obs_summary,
            "source_class": "OBSERVED",
            "universe": "keywords in our SERP snapshot database only",
            "last_snapshot": max((str(r["last_snapshot"]) for r in obs), default=None),
        },
        "provider": {
            **prov_summary,
            "source_class": "PROVIDER",
            "available": bool(prov),
            "last_snapshot": max((str(r["last_snapshot"]) for r in prov), default=None),
        },
        "actual": actual,
        "backlink_summary": backlink_summary,
        "indexed_pages": indexed_pages,
        "visibility": vis_payload,
        "organic_growth": growth_payload,
        "country_distribution": country_dist,
        "device_distribution": device_dist,
        "traffic_trends": trends_payload,
        "traffic_model": {
            "ctr_model": "ctr-curve-1",
            "typical_error": model_error,
            "note": "estimates are modeled; never added to GSC clicks",
        },
        "unsupported": {
            "clickstream": "not available without a licensed clickstream source",
            "audience": "not available",
            "all_channel_traffic": "not available",
        },
    }

    # Record analysis snapshot in PostgreSQL (DomainAnalysisRecord) for Domain History
    try:
        rec = DomainAnalysisRecord(
            org_id=org_id,
            site_id=own_site.id if own_site else None,
            domain=d,
            database=database,
            overview_summary={
                "observed_keywords": obs_summary["ranked_keywords"],
                "provider_keywords": prov_summary["ranked_keywords"],
                "estimated_traffic": obs_summary["estimated_traffic"]["value"],
                "estimated_traffic_value": obs_summary["estimated_traffic_value"]["value"],
                "visibility_score": vis_payload["score_pct"],
                "referring_domains": backlink_summary["referring_domains"],
                "total_backlinks": backlink_summary["total_backlinks"],
                "indexed_pages": indexed_pages["total_known_urls"],
            },
            technical_summary={},
            data_source="Domain_Intelligence_Engine",
            source_class="OBSERVED",
            is_estimated=True,
            confidence_score=0.8,
            confidence_level="HIGH",
        )
        db.add(rec)
        db.commit()
    except Exception:
        db.rollback()

    return result


def _parse_robots_detailed(content: str | None, status_code: int | None) -> dict:
    if not content:
        return {
            "reachable": status_code == 200,
            "status_code": status_code,
            "user_agents": [],
            "sitemaps_declared": [],
            "issues": ["robots.txt missing or empty"] if status_code != 200 else [],
            "raw_preview": "",
        }
    groups: list[dict] = []
    current: dict | None = None
    sitemaps: list[str] = []
    issues: list[str] = []

    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            issues.append(f"Line {line_no}: invalid syntax '{line[:60]}'")
            continue
        directive, val = [x.strip() for x in line.split(":", 1)]
        d_low = directive.lower()
        if d_low == "user-agent":
            current = {"user_agent": val, "allow": [], "disallow": [], "crawl_delay": None}
            groups.append(current)
        elif d_low == "allow":
            if current is None:
                current = {"user_agent": "*", "allow": [], "disallow": [], "crawl_delay": None}
                groups.append(current)
            current["allow"].append(val)
        elif d_low == "disallow":
            if current is None:
                current = {"user_agent": "*", "allow": [], "disallow": [], "crawl_delay": None}
                groups.append(current)
            current["disallow"].append(val)
            if val == "/" and current["user_agent"] in ("*", "googlebot", "Googlebot"):
                issues.append(f"Critical: User-agent '{current['user_agent']}' disallows entire site ('Disallow: /')")
            if any(ext in val.lower() for ext in (".css", ".js")):
                issues.append(f"Warning: robots.txt blocks CSS/JS resources ('Disallow: {val}'), which harms rendering")
        elif d_low == "crawl-delay":
            if current is not None:
                try:
                    current["crawl_delay"] = float(val)
                except ValueError:
                    issues.append(f"Line {line_no}: invalid Crawl-delay value '{val}'")
        elif d_low == "sitemap":
            sitemaps.append(val)

    if not sitemaps:
        issues.append("Notice: No 'Sitemap:' directive declared in robots.txt")

    return {
        "reachable": True,
        "status_code": status_code or 200,
        "user_agents": groups,
        "sitemaps_declared": sitemaps,
        "issues": issues,
        "raw_preview": content[:5000],
    }


def _parse_sitemap_xml_detailed(body: bytes, url: str) -> dict:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(maybe_gunzip(body), parser)
    except Exception as exc:
        return {"url": url, "valid_xml": False, "type": "invalid", "urls": [], "children": [], "lastmods": [], "error": str(exc)[:200]}

    tag = etree.QName(root).localname.lower()
    if tag == "sitemapindex":
        children = [(el.text or "").strip() for el in root.iter("{*}loc") if (el.text or "").strip()]
        return {"url": url, "valid_xml": True, "type": "sitemapindex", "urls": [], "children": children, "lastmods": [], "error": None}

    urls = []
    lastmods = []
    for url_el in root.iter("{*}url"):
        loc_el = url_el.find("{*}loc")
        lastmod_el = url_el.find("{*}lastmod")
        if loc_el is not None and (loc_el.text or "").strip():
            urls.append((loc_el.text or "").strip())
        if lastmod_el is not None and (lastmod_el.text or "").strip():
            lastmods.append((lastmod_el.text or "").strip()[:10])
    if not urls:
        urls = [(el.text or "").strip() for el in root.iter("{*}loc") if (el.text or "").strip()]
    return {"url": url, "valid_xml": True, "type": "urlset", "urls": urls, "children": [], "lastmods": lastmods, "error": None}


async def technical_analysis(
    db: Session,
    ch: ClickHouse,
    domain: str,
    org_id: int,
    fetch_live: bool = True,
    transport=None,
) -> dict:
    """Comprehensive Module 1 Domain Analysis:
    - Live / Cached Robots.txt analysis
    - Live / Cached XML Sitemap analysis
    - URL structure analysis (depth, length, parameters, slug issues)
    - Subdomain discovery & distribution
    - Indexability breakdown (Indexable vs Noindex, Canonical, Redirect, 4xx/5xx, Robots blocked)
    - Content distribution (Word count buckets, content types, subfolder categories)
    - Domain analysis history
    """
    d = registrable_domain(domain) or domain
    s = get_settings()

    # Check if we have a Site or Competitor and latest CrawlRun in DB
    own_site = next((st for st in db.scalars(select(Site)) if (registrable_domain(st.base_url.split("/")[2]) or "") == d), None)
    comp = next((c for c in db.scalars(select(Competitor)) if c.domain == d), None)

    latest_run = None
    if own_site is not None:
        latest_run = db.scalar(
            select(CrawlRun)
            .where(CrawlRun.site_id == own_site.id, CrawlRun.competitor_id.is_(None), CrawlRun.status == "completed")
            .order_by(CrawlRun.id.desc())
            .limit(1)
        )
    elif comp is not None:
        latest_run = db.scalar(
            select(CrawlRun)
            .where(CrawlRun.competitor_id == comp.id, CrawlRun.status == "completed")
            .order_by(CrawlRun.id.desc())
            .limit(1)
        )

    robots_text = latest_run.robots_txt if latest_run and latest_run.robots_txt else None
    robots_status = 200 if robots_text else None
    sitemaps_checked: list[dict] = []
    discovered_sitemap_urls: list[str] = list(latest_run.sitemap_urls or []) if latest_run else []
    lastmod_dates: list[str] = []

    if fetch_live:
        allowed = [d, f"www.{d}"]
        policy = FetchPolicy(
            allowed_hosts=allowed,
            user_agent=s.crawler_user_agent,
            timeout_s=8.0,
            max_bytes=5 * 1024 * 1024,
            max_redirects=4,
            allow_private=s.crawler_allow_private_networks,
            html_only=False,
        )
        try:
            async with build_client(policy, transport) as client:
                robots_url = f"https://{d}/robots.txt"
                r_res = await safe_get(client, robots_url, policy, accept_non_html=True)
                if r_res.status_code == 200 and r_res.body:
                    robots_text = r_res.body.decode("utf-8", "replace")
                    robots_status = 200
                elif r_res.status_code is not None:
                    robots_status = r_res.status_code

                declared = robots_sitemaps(robots_text) if robots_text else []
                sm_candidates = list(dict.fromkeys([*declared, f"https://{d}/sitemap.xml"]))[:5]
                for sm_url in sm_candidates:
                    n_sm = normalize_url(sm_url)
                    if not n_sm or host_of(n_sm) not in allowed:
                        continue
                    sm_res = await safe_get(client, n_sm, policy, accept_non_html=True)
                    if sm_res.status_code == 200 and sm_res.body:
                        parsed_sm = _parse_sitemap_xml_detailed(sm_res.body, n_sm)
                        sitemaps_checked.append({
                            "url": n_sm,
                            "status_code": 200,
                            "valid_xml": parsed_sm["valid_xml"],
                            "type": parsed_sm["type"],
                            "url_count": len(parsed_sm["urls"]),
                            "child_sitemaps_count": len(parsed_sm["children"]),
                            "error": parsed_sm["error"],
                        })
                        discovered_sitemap_urls.extend(parsed_sm["urls"])
                        lastmod_dates.extend(parsed_sm["lastmods"])
                        for child_url in parsed_sm["children"][:3]:
                            n_child = normalize_url(child_url)
                            if n_child and host_of(n_child) in allowed:
                                c_res = await safe_get(client, n_child, policy, accept_non_html=True)
                                if c_res.status_code == 200 and c_res.body:
                                    c_parsed = _parse_sitemap_xml_detailed(c_res.body, n_child)
                                    sitemaps_checked.append({
                                        "url": n_child,
                                        "status_code": 200,
                                        "valid_xml": c_parsed["valid_xml"],
                                        "type": c_parsed["type"],
                                        "url_count": len(c_parsed["urls"]),
                                        "child_sitemaps_count": len(c_parsed["children"]),
                                        "error": c_parsed["error"],
                                    })
                                    discovered_sitemap_urls.extend(c_parsed["urls"])
                                    lastmod_dates.extend(c_parsed["lastmods"])
                    elif sm_res.status_code is not None:
                        sitemaps_checked.append({
                            "url": n_sm,
                            "status_code": sm_res.status_code,
                            "valid_xml": False,
                            "type": "missing",
                            "url_count": 0,
                            "child_sitemaps_count": 0,
                            "error": sm_res.error or f"HTTP {sm_res.status_code}",
                        })
        except Exception:
            pass

    discovered_sitemap_urls = list(dict.fromkeys(discovered_sitemap_urls))
    robots_analysis = _parse_robots_detailed(robots_text, robots_status)

    sitemap_issues: list[str] = []
    if not discovered_sitemap_urls and not (latest_run and latest_run.sitemap_found):
        sitemap_issues.append("No reachable XML sitemap or sitemap URLs discovered")
    for sm in sitemaps_checked:
        if sm.get("error"):
            sitemap_issues.append(f"{sm['url']}: {sm['error']}")

    sitemap_analysis = {
        "sitemap_found": bool(discovered_sitemap_urls) or bool(latest_run and latest_run.sitemap_found),
        "sitemaps_checked": sitemaps_checked,
        "total_urls": len(discovered_sitemap_urls),
        "sample_urls": discovered_sitemap_urls[:50],
        "lastmod_stats": {
            "with_lastmod": len(lastmod_dates),
            "oldest_lastmod": min(lastmod_dates) if lastmod_dates else None,
            "newest_lastmod": max(lastmod_dates) if lastmod_dates else None,
        },
        "issues": sitemap_issues,
        **make_provenance("Internal_Crawler_Sitemap_Parser", "OBSERVED", None, None, False, 0.9),
    }

    # Crawl pages if available
    crawl_pages_rows: list[CrawlPage] = []
    if latest_run is not None:
        crawl_pages_rows = list(db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == latest_run.id)).all())

    # Collect all known URLs for URL structure & Subdomain analysis
    serp_urls_and_hosts: list[tuple[str, str]] = []
    try:
        s_rows = ch.query(
            "SELECT DISTINCT url, domain FROM serp_results WHERE registrable_domain = {d:String} AND url != '' LIMIT 2000",
            {"d": d},
        )
        serp_urls_and_hosts = [(r["url"], r.get("domain") or host_of(r["url"])) for r in s_rows]
    except Exception:
        serp_urls_and_hosts = []

    all_urls_set: set[str] = set(discovered_sitemap_urls)
    for p in crawl_pages_rows:
        if p.normalized_url:
            all_urls_set.add(p.normalized_url)
    for u, _h in serp_urls_and_hosts:
        all_urls_set.add(u)

    all_urls = sorted(all_urls_set)
    lengths = [len(u) for u in all_urls]
    depth_buckets = {"1": 0, "2": 0, "3": 0, "4+": 0}
    param_urls = 0
    slug_issues = 0
    sample_slug_issues: list[str] = []

    for u in all_urls:
        sp = urlsplit(u)
        segs = [seg for seg in sp.path.split("/") if seg]
        d_len = len(segs)
        if d_len <= 1:
            depth_buckets["1"] += 1
        elif d_len == 2:
            depth_buckets["2"] += 1
        elif d_len == 3:
            depth_buckets["3"] += 1
        else:
            depth_buckets["4+"] += 1
        if sp.query:
            param_urls += 1
        if "_" in sp.path or any(c.isupper() for c in sp.path):
            slug_issues += 1
            if len(sample_slug_issues) < 10:
                sample_slug_issues.append(u)

    url_structure = {
        "total_analyzed_urls": len(all_urls),
        "avg_url_length": round(sum(lengths) / len(lengths), 1) if lengths else None,
        "max_url_length": max(lengths) if lengths else None,
        "long_urls_count": sum(l > 100 for l in lengths),
        "depth_distribution": depth_buckets,
        "parameter_urls_count": param_urls,
        "slug_issues_count": slug_issues,
        "sample_slug_issues": sample_slug_issues,
        **make_provenance("URL_Structure_Analyzer", "OBSERVED", None, None, False, 0.9 if all_urls else 0.4),
    }

    # Subdomain analysis
    sub_urls: Counter[str] = Counter()
    for u in all_urls:
        h = host_of(u)
        if h and (h == d or h.endswith("." + d)):
            sub_urls[h] += 1
    sub_kws: Counter[str] = Counter()
    try:
        sk_rows = ch.query(
            "SELECT domain, uniqExact(keyword_key) AS kws FROM serp_results "
            "WHERE registrable_domain = {d:String} AND result_type = 'organic' GROUP BY domain",
            {"d": d},
        )
        for r in sk_rows:
            if r.get("domain"):
                sub_kws[r["domain"]] = int(r["kws"])
                if r["domain"] not in sub_urls:
                    sub_urls[r["domain"]] = 0
    except Exception:
        pass

    subdomains = [
        {
            "subdomain": sub,
            "is_root_or_www": sub in (d, f"www.{d}"),
            "urls_count": cnt,
            "ranked_keywords": sub_kws.get(sub, 0),
        }
        for sub, cnt in sub_urls.most_common(25)
    ]

    # Indexability analysis
    indexable_cnt = 0
    noindex_cnt = 0
    canonicalized_cnt = 0
    redirect_3xx_cnt = 0
    error_4xx_cnt = 0
    error_5xx_cnt = 0
    robots_blocked_cnt = len(latest_run.robots_blocked_urls or []) if latest_run else 0

    for p in crawl_pages_rows:
        code = p.status_code or 0
        robots_directives = f"{p.meta_robots or ''} {p.x_robots_tag or ''}".lower()
        if 300 <= code < 400 or p.redirect_chain:
            redirect_3xx_cnt += 1
        if 400 <= code < 500:
            error_4xx_cnt += 1
            continue
        if code >= 500 or p.fetch_error:
            error_5xx_cnt += 1
            continue
        if "noindex" in robots_directives:
            noindex_cnt += 1
            continue
        if p.canonical_url and p.canonical_url != p.normalized_url and p.canonical_url != p.final_url:
            canonicalized_cnt += 1
            continue
        if code == 200 and p.is_html:
            indexable_cnt += 1

    total_crawled = len(crawl_pages_rows)
    non_indexable_cnt = noindex_cnt + canonicalized_cnt + error_4xx_cnt + error_5xx_cnt + robots_blocked_cnt
    indexability = {
        "crawl_available": latest_run is not None,
        "crawl_id": latest_run.id if latest_run else None,
        "total_pages": total_crawled + robots_blocked_cnt,
        "indexable_count": indexable_cnt,
        "non_indexable_count": non_indexable_cnt,
        "indexable_pct": round(indexable_cnt / max(1, indexable_cnt + non_indexable_cnt) * 100, 1)
        if (indexable_cnt + non_indexable_cnt) > 0
        else None,
        "breakdown": {
            "indexable": indexable_cnt,
            "noindex": noindex_cnt,
            "canonicalized": canonicalized_cnt,
            "redirect_3xx": redirect_3xx_cnt,
            "client_error_4xx": error_4xx_cnt,
            "server_error_5xx": error_5xx_cnt,
            "robots_blocked": robots_blocked_cnt,
        },
        **make_provenance("Internal_Crawler", "OBSERVED", latest_run.finished_at.isoformat() if latest_run and latest_run.finished_at else None, None, False, 0.95 if latest_run else 0.4),
    }

    # Content distribution
    wc_buckets = {"thin_lt_300": 0, "medium_300_800": 0, "comprehensive_800_1500": 0, "in_depth_1500_plus": 0}
    wc_values: list[int] = []
    content_types: Counter[str] = Counter()
    cat_folders: Counter[str] = Counter()
    cat_words: dict[str, list[int]] = defaultdict(list)

    for p in crawl_pages_rows:
        ctype = (p.content_type or "unknown").split(";")[0].strip().lower()
        content_types[ctype] += 1
        robots_dir = f"{p.meta_robots or ''} {p.x_robots_tag or ''}".lower()
        if p.status_code == 200 and p.is_html and "noindex" not in robots_dir and p.word_count is not None:
            wc = int(p.word_count)
            wc_values.append(wc)
            if wc < 300:
                wc_buckets["thin_lt_300"] += 1
            elif wc < 800:
                wc_buckets["medium_300_800"] += 1
            elif wc < 1500:
                wc_buckets["comprehensive_800_1500"] += 1
            else:
                wc_buckets["in_depth_1500_plus"] += 1
        sp = urlsplit(p.normalized_url or p.url)
        segs = [seg for seg in sp.path.split("/") if seg]
        cat = f"/{segs[0]}" if segs else "/"
        cat_folders[cat] += 1
        if p.word_count is not None:
            cat_words[cat].append(int(p.word_count))

    if not crawl_pages_rows:
        for u in all_urls:
            sp = urlsplit(u)
            segs = [seg for seg in sp.path.split("/") if seg]
            cat = f"/{segs[0]}" if segs else "/"
            cat_folders[cat] += 1

    content_distribution = {
        "word_count_buckets": wc_buckets,
        "avg_word_count": round(sum(wc_values) / len(wc_values), 1) if wc_values else None,
        "content_types": [{"content_type": ct, "count": n} for ct, n in content_types.most_common(10)],
        "category_subfolders": [
            {
                "category": cat,
                "pages_count": cnt,
                "avg_word_count": round(sum(cat_words[cat]) / len(cat_words[cat]), 1) if cat_words.get(cat) else None,
            }
            for cat, cnt in cat_folders.most_common(15)
        ],
        **make_provenance("Internal_Content_Profiler", "OBSERVED", None, None, False, 0.9 if crawl_pages_rows else 0.5),
    }

    # Persist technical summary in DomainAnalysisRecord
    tech_summary_compact = {
        "robots_reachable": robots_analysis["reachable"],
        "robots_issues_count": len(robots_analysis["issues"]),
        "sitemap_found": sitemap_analysis["sitemap_found"],
        "sitemap_urls_count": sitemap_analysis["total_urls"],
        "total_analyzed_urls": url_structure["total_analyzed_urls"],
        "subdomains_count": len(subdomains),
        "indexable_count": indexability["indexable_count"],
        "non_indexable_count": indexability["non_indexable_count"],
    }
    try:
        rec = DomainAnalysisRecord(
            org_id=org_id,
            site_id=own_site.id if own_site else None,
            domain=d,
            database="technical",
            overview_summary={},
            technical_summary=tech_summary_compact,
            data_source="Internal_Crawler",
            source_class="OBSERVED",
            is_estimated=False,
            confidence_score=0.9,
            confidence_level="HIGH",
        )
        db.add(rec)
        db.commit()
    except Exception:
        db.rollback()

    history_rows = db.scalars(
        select(DomainAnalysisRecord)
        .where(DomainAnalysisRecord.org_id == org_id, DomainAnalysisRecord.domain == d)
        .order_by(DomainAnalysisRecord.id.desc())
        .limit(20)
    ).all()

    domain_history = [
        {
            "id": r.id,
            "database": r.database,
            "overview_summary": r.overview_summary,
            "technical_summary": r.technical_summary,
            "data_source": r.data_source,
            "source_class": r.source_class,
            "is_estimated": r.is_estimated,
            "confidence_score": r.confidence_score,
            "confidence_level": r.confidence_level,
            "collected_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in history_rows
    ]

    return {
        "domain": d,
        "robots_txt": robots_analysis,
        "sitemap": sitemap_analysis,
        "url_structure": url_structure,
        "subdomains": subdomains,
        "page_count": {
            "total_discovered_urls": len(all_urls),
            "crawled_pages": total_crawled,
            "sitemap_urls": len(discovered_sitemap_urls),
            "serp_urls": len(serp_urls_and_hosts),
        },
        "indexability": indexability,
        "content_distribution": content_distribution,
        "domain_history": domain_history,
        "provenance": make_provenance("Internal_Crawler", "OBSERVED", None, None, False, 0.9),
    }


def calibrate_ctr(db: Session, site_id: int, days: int = 90) -> dict:
    """Site-specific CTR-by-position curve from own GSC (query rows) + error of the static curve vs actual clicks."""
    since = date.today() - timedelta(days=days)
    rows = db.execute(
        select(
            SearchPerformanceDaily.query,
            func.sum(SearchPerformanceDaily.clicks),
            func.sum(SearchPerformanceDaily.impressions),
            func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions),
        )
        .where(SearchPerformanceDaily.site_id == site_id, SearchPerformanceDaily.date >= since)
        .group_by(SearchPerformanceDaily.query)
    ).all()
    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    apes = []
    for _q, clicks, impr, wpos in rows:
        if not impr or impr < CALIBRATION_MIN_IMPRESSIONS:
            continue
        pos = max(1, round(wpos / impr))
        if pos > 20:
            continue
        buckets[pos][0] += int(clicks)
        buckets[pos][1] += int(impr)
        expected = impr * ctr(pos)  # static curve applied to observed impressions
        if clicks:
            apes.append(abs(expected - clicks) / clicks)
    curve = {p: round(c / i, 4) for p, (c, i) in sorted(buckets.items()) if i >= CALIBRATION_MIN_IMPRESSIONS}
    if not curve:
        return {"available": False, "reason": f"needs queries with ≥{CALIBRATION_MIN_IMPRESSIONS} impressions in top 20"}
    return {
        "available": True,
        "model_version": f"ctr-calibrated-1:site{site_id}",
        "curve": curve,
        "static_curve": {p: CTR_CURVE.get(p, 0.01) for p in curve},
        "median_ape": round(statistics.median(apes), 3) if apes else None,
        "mape": round(sum(apes) / len(apes), 3) if apes else None,
        "queries_used": len(apes),
        "note": "Error of the static curve against own GSC clicks; tenant-only (no cross-tenant data used).",
    }


def calibrate_ctr_cross_tenant(sysdb: Session, opt_in_org_ids: list[int], min_k: int = 5, days: int = 90) -> dict:
    """Cross-tenant aggregate CTR calibration (§0.3 rule 6, §7): strictly limited to opted-in organizations and
    enforces k-anonymity (a position bucket is emitted only if ≥ min_k distinct opted-in organizations contribute)."""
    if min_k < 2:
        raise ValueError("min_k must be ≥ 2 for k-anonymity")
    opted = sorted(set(opt_in_org_ids))
    if len(opted) < min_k:
        return {
            "available": False,
            "k_anonymity_threshold": min_k,
            "opted_in_tenants": len(opted),
            "reason": f"requires ≥{min_k} opted-in organizations (have {len(opted)})",
        }
    since = date.today() - timedelta(days=days)
    rows = sysdb.execute(
        select(
            Site.org_id,
            SearchPerformanceDaily.query,
            func.sum(SearchPerformanceDaily.clicks),
            func.sum(SearchPerformanceDaily.impressions),
            func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions),
        )
        .join(Site, Site.id == SearchPerformanceDaily.site_id)
        .where(Site.org_id.in_(opted), SearchPerformanceDaily.date >= since)
        .group_by(Site.org_id, SearchPerformanceDaily.query)
    ).all()
    buckets_clicks: dict[int, int] = defaultdict(int)
    buckets_impr: dict[int, int] = defaultdict(int)
    buckets_orgs: dict[int, set[int]] = defaultdict(set)
    for org_id, _q, clicks, impr, wpos in rows:
        if not impr or impr < CALIBRATION_MIN_IMPRESSIONS:
            continue
        pos = max(1, round(wpos / impr))
        if pos > 20:
            continue
        buckets_clicks[pos] += int(clicks)
        buckets_impr[pos] += int(impr)
        buckets_orgs[pos].add(int(org_id))

    curve = {
        pos: round(buckets_clicks[pos] / buckets_impr[pos], 4)
        for pos in sorted(buckets_impr)
        if len(buckets_orgs[pos]) >= min_k and buckets_impr[pos] >= CALIBRATION_MIN_IMPRESSIONS
    }
    suppressed = [pos for pos in sorted(buckets_impr) if len(buckets_orgs[pos]) < min_k]
    return {
        "available": bool(curve),
        "model_version": f"ctr-cross-tenant-k{min_k}",
        "k_anonymity_threshold": min_k,
        "opted_in_tenants": len(opted),
        "curve": curve,
        "suppressed_positions_below_k": suppressed,
        "note": f"Aggregate across opted-in tenants only; positions with <{min_k} distinct tenants are suppressed (k-anonymity).",
    }


def keyword_gap(ch: ClickHouse, domains: list[str], database: str, source: str = "observed", limit: int = 500) -> dict:
    ds = [registrable_domain(d) or d for d in domains]
    if not 2 <= len(set(ds)) <= 5:
        raise ValueError("keyword gap needs 2–5 distinct domains")
    fetch = observed_rankings if source == "observed" else provider_rankings
    pos: dict[str, dict[str, dict]] = defaultdict(dict)
    for d in ds:
        for r in fetch(ch, d, database):
            pos[r["keyword_key"]][d] = {"position": int(r["position"]), "url": r.get("url"), "date": str(r["last_snapshot"])}
    you, others = ds[0], ds[1:]
    cats: dict[str, list] = {"shared": [], "missing": [], "weak": [], "strong": [], "unique": [], "untapped": []}
    vols = _volumes(ch, database, list(pos))
    for k, by in pos.items():
        mine = by.get(you, {}).get("position")
        theirs = [by[o]["position"] for o in others if o in by]
        row = {"keyword_key": k, "search_volume": vols.get(k), "positions": by}
        if mine and len(theirs) == len(others):
            cats["shared"].append(row)
        if not mine and len(theirs) == len(others):
            cats["missing"].append(row)
        elif not mine and theirs:
            cats["untapped"].append(row)
        if mine and theirs and mine > max(theirs):
            cats["weak"].append(row)
        if mine and theirs and mine < min(theirs):
            cats["strong"].append(row)
        if mine and not theirs:
            cats["unique"].append(row)
    for v in cats.values():
        v.sort(key=lambda r: (-(r["search_volume"] or -1), r["keyword_key"]))
    return {
        "you": you,
        "competitors": others,
        "database": database,
        "source": source,
        "source_class": "OBSERVED" if source == "observed" else "PROVIDER",
        "counts": {k: len(v) for k, v in cats.items()},
        "keywords": {k: v[:limit] for k, v in cats.items()},
        "definitions": {
            "shared": "all domains rank",
            "missing": "all competitors rank, you don't",
            "untapped": "some competitors rank, you don't",
            "weak": "you rank below every competitor",
            "strong": "you rank above every competitor",
            "unique": "only you rank",
        },
    }


def own_gsc_layer(db: Session, site_id: int, keys: set[str], lang: str) -> dict:
    """First-party layer for gap keywords: shown next to the gap, never merged into provider positions."""
    since = date.today() - timedelta(days=90)
    out = {}
    for q, impr, wpos in db.execute(
        select(
            SearchPerformanceDaily.query,
            func.sum(SearchPerformanceDaily.impressions),
            func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions),
        )
        .where(SearchPerformanceDaily.site_id == site_id, SearchPerformanceDaily.date >= since)
        .group_by(SearchPerformanceDaily.query)
    ):
        k = match_key(q, lang)
        if k in keys:
            out[k] = {"impressions_90d": int(impr), "avg_position": round(wpos / impr, 1) if impr else None}
    return {"source_class": "FIRST_PARTY", "by_keyword": out}


def serp_competitor_suggestions(ch: ClickHouse, own_domain: str, database: str, limit: int = 20) -> list[dict]:
    """Domains that co-occur in top-10 of keywords where the own domain ranks (evidence: shared keywords). Suggestions only."""
    rows = ch.query(
        "WITH mine AS (SELECT DISTINCT keyword_key FROM domain_keyword_rankings FINAL WHERE registrable_domain = {d:String} "
        "AND locale = {db:String} AND best_rank <= 10) "
        "SELECT registrable_domain AS domain, uniqExact(keyword_key) AS shared, groupUniqArray(5)(keyword_key) AS sample "
        "FROM domain_keyword_rankings FINAL WHERE locale = {db:String} AND best_rank <= 10 AND registrable_domain != {d:String} "
        "AND keyword_key IN (SELECT keyword_key FROM mine) GROUP BY domain ORDER BY shared DESC, domain LIMIT {lim:UInt32}",
        {"d": own_domain, "db": database, "lim": limit},
    )
    return [{"domain": r["domain"], "shared_top10_keywords": int(r["shared"]), "sample_keywords": r["sample"]} for r in rows]


async def fetch_ranked_keywords(
    db: Session,
    ch: ClickHouse,
    org_id: int,
    domain: str,
    database: str,
    limit: int,
    confirmed: bool,
    request_id: str,
) -> dict:
    d = registrable_domain(domain) or domain
    provider = registry.get("ranked_keywords", db, org_id)
    kw_service._charge_or_preflight(db, org_id, "keyword_lookup", limit, confirmed, f"ranked:{request_id}")
    ctx = SearchContext(database)
    res = await runtime.call(
        db,
        provider,
        "ranked_keywords",
        {"d": d, "db": database, "limit": limit},
        lambda: provider.ranked_keywords(ctx, d, limit),
        org_id=org_id,
        archive=kw_service._archive(),
    )
    lang = database.split("-")[0]
    v = store._version()
    rows = [
        {
            "registrable_domain": d,
            "locale": database,
            "keyword": r["keyword"],
            "keyword_key": match_key(r["keyword"], lang),
            "position": r["position"],
            "url": r["url"],
            "search_volume": r["search_volume"],
            "snapshot_date": date.today().isoformat(),
            "source_class": r["lineage"]["source_class"],
            "provider": r["lineage"]["provider"],
            "provider_request_id": r["lineage"].get("provider_request_id") or "",
            "fetched_at": store._ts(r["lineage"].get("fetched_at")),
            "is_synthetic": int(r["lineage"].get("is_synthetic", False)),
            "version": v,
        }
        for r in res["data"]
    ]
    ch.insert("domain_ranked_keywords", rows)
    return {"domain": d, "ingested": len(rows), "cached": res["cached"], "provider": res["provider"]}


def competitor_digest(db: Session, competitor_id: int) -> dict:
    """Diff of the last two completed crawls of a competitor (OBSERVED public HTML)."""
    runs = db.scalars(
        select(CrawlRun)
        .where(CrawlRun.competitor_id == competitor_id, CrawlRun.status == "completed")
        .order_by(CrawlRun.id.desc())
        .limit(2)
    ).all()
    if len(runs) < 2:
        return {"available": False, "reason": "needs two completed crawls of this competitor"}
    cur, prev = runs

    def pages(run):
        return {p.normalized_url: p for p in db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == run.id))}

    a, b = pages(prev), pages(cur)
    new, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    title_changes = [{"url": u, "from": a[u].title, "to": b[u].title} for u in sorted(set(a) & set(b)) if a[u].title != b[u].title]
    pricing = [c for c in title_changes if "pric" in c["url"] or "fiyat" in c["url"]]
    pricing += [
        {"url": u, "content_changed": True}
        for u in sorted(set(a) & set(b))
        if ("pric" in u or "fiyat" in u) and a[u].content_hash != b[u].content_hash and u not in {c["url"] for c in pricing}
    ]
    sm_prev, sm_cur = set(prev.sitemap_urls or []), set(cur.sitemap_urls or [])
    days = max(1, (cur.finished_at - prev.finished_at).days) if cur.finished_at and prev.finished_at else None
    return {
        "available": True,
        "source_class": "OBSERVED",
        "from_crawl": prev.id,
        "to_crawl": cur.id,
        "period": {
            "from": prev.finished_at.isoformat() if prev.finished_at else None,
            "to": cur.finished_at.isoformat() if cur.finished_at else None,
        },
        "new_urls": new[:200],
        "removed_urls": removed[:200],
        "title_changes": title_changes[:200],
        "pricing_changes": pricing,
        "sitemap": {"added": sorted(sm_cur - sm_prev)[:200], "removed": sorted(sm_prev - sm_cur)[:200]},
        "publishing_velocity_per_day": round(len(new) / days, 2) if days else None,
        "caveat": "Based on bounded crawls (max_pages); unchanged pages outside the crawl sample are not observed.",
    }


GAP_TARGET_POSITION = 5
GAP_REACH_PROBABILITY = 0.3  # uncalibrated prior; replaced by measured hit-rate once rank tracking history exists
GAP_MAX_KD = 60


def keyword_gap_opportunities(db: Session, ch: ClickHouse, site: Site, gap: dict) -> dict:
    """Turn 'missing'/'untapped' gap keywords into opportunities when intent, KD and volume evidence exist."""
    lang = gap["database"].split("-")[0]
    keys = [r["keyword_key"] for c in ("missing", "untapped") for r in gap["keywords"][c]]
    meta = (
        {
            r["keyword_key"]: r
            for r in ch.query(
                "SELECT keyword_key, keyword, search_volume, kd, intents FROM keyword_latest FINAL WHERE locale = {db:String} "
                "AND has({k:Array(String)}, keyword_key)",
                {"db": gap["database"], "k": keys},
            )
        }
        if keys
        else {}
    )
    own_layer = own_gsc_layer(db, site.id, set(keys), lang)["by_keyword"]
    created = updated = skipped = 0
    now = datetime.now(UTC)
    for cat in ("missing", "untapped"):
        for r in gap["keywords"][cat]:
            m = meta.get(r["keyword_key"])
            if (
                not m
                or m["search_volume"] is None
                or m["kd"] is None
                or float(m["kd"]) > GAP_MAX_KD
                or not set(m["intents"]) & {"commercial", "transactional"}
            ):
                skipped += 1
                continue
            vol = int(m["search_volume"])
            p50 = vol * ctr(GAP_TARGET_POSITION) * GAP_REACH_PROBABILITY
            key = f"keyword_gap|{r['keyword_key']}"
            o = db.scalar(select(GrowthOpportunity).where(GrowthOpportunity.site_id == site.id, GrowthOpportunity.key == key))
            if o is None:
                o = GrowthOpportunity(site_id=site.id, key=key, status="new")
                db.add(o)
                created += 1
            else:
                updated += 1
            o.url, o.opportunity_type = None, "keyword_gap"
            o.title = f"Keyword gap: “{m['keyword']}” ({cat})"
            o.description = (
                f"Competitors rank for “{m['keyword']}” and you do not. Search volume {vol}/month (provider), "
                f"KD {m['kd']} (kd-1), intent {', '.join(m['intents'])}."
            )
            o.evidence = {
                "category": gap["definitions"][cat],
                "competitor_positions": r["positions"],
                "search_volume": vol,
                "kd": float(m["kd"]),
                "intents": m["intents"],
                "gap_source": gap["source_class"],
                "own_gsc": own_layer.get(r["keyword_key"]),
            }
            o.data_sources = [f"{gap['source_class'].lower()}:serp", "provider:keyword_metrics", "model:kd-1"] + (
                ["gsc:own"] if own_layer.get(r["keyword_key"]) else []
            )
            o.date_range = {
                "start": min(v["date"] for v in r["positions"].values()),
                "end": max(v["date"] for v in r["positions"].values()),
            }
            o.impact_estimate = {
                "metric": "monthly_clicks",
                "p10": round(p50 * 0.3, 1),
                "p50": round(p50, 1),
                "p90": round(p50 * 2.5, 1),
                "value": round(p50, 1),
                "unit": "clicks/month",
                "source_class": "MODELED",
                "method": f"volume × CTR(pos {GAP_TARGET_POSITION}, ctr-curve-1) × reach probability {GAP_REACH_PROBABILITY} (uncalibrated)",
            }
            o.confidence = 0.35 if own_layer.get(r["keyword_key"]) else 0.25
            o.effort = "high" if cat == "missing" and float(m["kd"]) > 40 else "medium"
            o.priority_score = round(min(100.0, (p50**0.5)) * o.confidence * (0.7 if o.effort == "medium" else 0.4), 2)
            o.recommended_action = "Create or expand a page that matches the search intent; map it in the keyword strategy and track it."
            o.validation_metric = "Tracked rank and GSC clicks for this keyword 8–12 weeks after publishing"
            o.limitations = (
                "Competitors ranking does not prove the keyword is attainable for you; KD lacks the authority component "
                "until the backlink index exists; reach probability is an uncalibrated prior."
            )
            o.last_seen_at = now
    db.commit()
    return {"created": created, "updated": updated, "skipped_insufficient_evidence": skipped}
