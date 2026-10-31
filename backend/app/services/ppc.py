"""Module 9: PPC Intelligence & Organic vs Paid Hybrid Strategy Engine."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.clickhouse import ClickHouse
from app.models import Competitor, PpcKeywordRecord, Site
from app.normalize.domains import registrable_domain
from app.normalize.text import match_key
from app.services.crawler.urls import host_of
from app.services.rank_tracking import ctr

PAID_CTR_CURVE = {1: 0.045, 2: 0.030, 3: 0.020, 4: 0.012}


def paid_ctr(pos: int) -> float:
    return PAID_CTR_CURVE.get(pos, 0.008)


def _ppc_out(r: PpcKeywordRecord) -> dict:
    now_iso = datetime.now(UTC).isoformat()
    col_iso = r.created_at.isoformat() if r.created_at else now_iso
    upd_iso = r.updated_at.isoformat() if r.updated_at else col_iso
    return {
        "id": r.id,
        "site_id": r.site_id,
        "domain": r.domain,
        "keyword": r.keyword,
        "keyword_key": r.keyword_key,
        "database": r.database,
        "position": r.position,
        "search_volume": r.search_volume,
        "estimated_cpc": r.estimated_cpc,
        "competition": r.competition,
        "ad_headline": r.ad_headline,
        "ad_description": r.ad_description,
        "landing_page": r.landing_page,
        "estimated_monthly_clicks": r.estimated_monthly_clicks,
        "estimated_monthly_cost": r.estimated_monthly_cost,
        "provenance": {
            "dataSource": r.data_source,
            "data_source": r.data_source,
            "source_class": r.source_class,
            "collectedAt": col_iso,
            "collected_at": col_iso,
            "updatedAt": upd_iso,
            "updated_at": upd_iso,
            "isEstimated": r.is_estimated,
            "is_estimated": r.is_estimated,
            "confidenceScore": r.confidence_score,
            "confidence_score": r.confidence_score,
            "confidenceLevel": "HIGH" if r.confidence_score >= 0.75 else "MEDIUM",
            "confidence_level": "HIGH" if r.confidence_score >= 0.75 else "MEDIUM",
        },
    }


def sync_paid_serps_from_clickhouse(db: Session, ch: ClickHouse | None, site: Site, database: str) -> int:
    """Extract observed paid SERP items from ClickHouse `serp_results` into `PpcKeywordRecord`."""
    if ch is None:
        return 0
    lang = database.split("-")[0]
    try:
        paid_rows = ch.query(
            "SELECT keyword_key, any(keyword) AS kw, registrable_domain AS dom, argMin(rank_group, rank_group) AS pos, "
            "any(url) AS landing, any(title) AS headline, any(snippet) AS descr "
            "FROM serp_results WHERE locale = {db:String} AND result_type = 'paid' AND registrable_domain != '' "
            "GROUP BY keyword_key, dom LIMIT 300",
            {"db": database},
        )
    except Exception:
        paid_rows = []
    if not paid_rows:
        return 0

    keys = list({r["keyword_key"] for r in paid_rows})
    meta_rows = ch.query(
        "SELECT keyword_key, search_volume, cpc, kd FROM keyword_latest FINAL WHERE locale = {db:String} AND has({k:Array(String)}, keyword_key)",
        {"db": database, "k": keys},
    )
    meta = {r["keyword_key"]: r for r in meta_rows}

    ingested = 0
    for r in paid_rows:
        kk = r["keyword_key"]
        m = meta.get(kk, {})
        vol = int(m["search_volume"]) if m.get("search_volume") is not None else None
        cpc = float(m["cpc"]) if m.get("cpc") is not None else None
        pos = int(r["pos"] or 1)
        est_clicks = round(vol * paid_ctr(pos), 1) if vol is not None else None
        est_cost = round(est_clicks * cpc, 2) if (est_clicks is not None and cpc is not None) else None
        existing = db.scalar(
            select(PpcKeywordRecord).where(
                PpcKeywordRecord.org_id == site.org_id,
                PpcKeywordRecord.domain == r["dom"],
                PpcKeywordRecord.keyword_key == kk,
                PpcKeywordRecord.database == database,
            )
        )
        if existing is None:
            existing = PpcKeywordRecord(
                org_id=site.org_id,
                site_id=site.id,
                domain=r["dom"],
                keyword=r.get("kw") or kk,
                keyword_key=match_key(r.get("kw") or kk, lang),
                database=database,
                position=pos,
                search_volume=vol,
                estimated_cpc=cpc,
                competition=0.7,
                ad_headline=(r.get("headline") or "")[:500] or None,
                ad_description=r.get("descr") or None,
                landing_page=r.get("landing") or None,
                estimated_monthly_clicks=est_clicks,
                estimated_monthly_cost=est_cost,
                is_estimated=True,
                data_source="ClickHouse_SERP_Paid_Ads",
                source_class="OBSERVED",
                confidence_score=0.75,
            )
            db.add(existing)
            ingested += 1
    if ingested:
        db.commit()
    return ingested


def ppc_overview(db: Session, ch: ClickHouse | None, site: Site, database: str = "tr-TR:google.com.tr") -> dict:
    """Comprehensive PPC Intelligence: Competitor paid keywords, ad copies, overlap & Organic vs PPC Hybrid Strategy."""
    sync_paid_serps_from_clickhouse(db, ch, site, database)
    now_iso = datetime.now(UTC).isoformat()
    own_domain = registrable_domain(host_of(site.base_url)) or host_of(site.base_url).removeprefix("www.")
    comps = db.scalars(select(Competitor).where(Competitor.site_id == site.id, Competitor.is_active.is_(True)).order_by(Competitor.id)).all()
    comp_domains = [c.domain for c in comps]
    tracked_domains = [own_domain, *comp_domains]

    all_records = db.scalars(
        select(PpcKeywordRecord)
        .where(PpcKeywordRecord.org_id == site.org_id)
        .order_by(PpcKeywordRecord.estimated_monthly_cost.desc().nulls_last(), PpcKeywordRecord.id.desc())
    ).all()

    for r in all_records:
        if r.estimated_monthly_clicks is None and r.search_volume is not None:
            r.estimated_monthly_clicks = round(r.search_volume * paid_ctr(r.position or 1), 1)
        if r.estimated_monthly_cost is None and r.estimated_monthly_clicks is not None and r.estimated_cpc is not None:
            r.estimated_monthly_cost = round(r.estimated_monthly_clicks * r.estimated_cpc, 2)

    by_domain: dict[str, list[PpcKeywordRecord]] = defaultdict(list)
    by_kw: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in all_records:
        by_domain[r.domain].append(r)
        by_kw[r.keyword_key][r.domain] = {
            "keyword": r.keyword,
            "position": r.position,
            "cpc": r.estimated_cpc,
            "volume": r.search_volume,
            "ad_headline": r.ad_headline,
            "landing_page": r.landing_page,
        }

    domain_summaries = []
    for dom in tracked_domains:
        recs = by_domain.get(dom, [])
        domain_summaries.append({
            "domain": dom,
            "is_own": dom == own_domain,
            "paid_keywords_count": len(recs),
            "estimated_monthly_paid_clicks": round(sum(float(x.estimated_monthly_clicks or 0) for x in recs), 1),
            "estimated_monthly_ad_spend_usd": round(sum(float(x.estimated_monthly_cost or 0) for x in recs), 2),
            "avg_cpc_usd": round(sum(float(x.estimated_cpc or 0) for x in recs if x.estimated_cpc) / max(1, sum(1 for x in recs if x.estimated_cpc)), 2) if any(x.estimated_cpc for x in recs) else None,
            "ad_copies": [
                {
                    "keyword": x.keyword,
                    "position": x.position,
                    "headline": x.ad_headline,
                    "description": x.ad_description,
                    "landing_page": x.landing_page,
                    "cpc_usd": x.estimated_cpc,
                }
                for x in recs if x.ad_headline or x.ad_description
            ][:20],
        })

    # Paid Keyword Overlap & Gap
    paid_missing = []
    paid_shared = []
    for kk, dom_map in by_kw.items():
        competitor_bidders = [d for d in dom_map if d != own_domain]
        sample = next(iter(dom_map.values()))
        entry = {
            "keyword": sample["keyword"],
            "keyword_key": kk,
            "volume": sample["volume"],
            "cpc_usd": sample["cpc"],
            "bidding_domains": dom_map,
        }
        if own_domain in dom_map and competitor_bidders:
            paid_shared.append(entry)
        elif own_domain not in dom_map and competitor_bidders:
            paid_missing.append(entry)

    # Organic vs PPC Hybrid Strategy Matrix
    high_cpc_organic_targets = []
    organic_saves_ppc_budget = []
    paid_bridges_high_kd_gap = []
    dual_serp_domination = []

    if ch is not None:
        try:
            org_rows = ch.query(
                "SELECT keyword_key, argMax(best_rank, snapshot_date) AS pos, argMax(url, snapshot_date) AS url "
                "FROM domain_keyword_rankings FINAL WHERE registrable_domain = {d:String} AND locale = {db:String} GROUP BY keyword_key",
                {"d": own_domain, "db": database},
            )
            own_org = {r["keyword_key"]: {"position": int(r["pos"]), "url": r["url"]} for r in org_rows}
            kw_rows = ch.query(
                "SELECT keyword_key, keyword, search_volume, cpc, kd, intents FROM keyword_latest FINAL WHERE locale = {db:String} LIMIT 500",
                {"db": database},
            )
        except Exception:
            own_org, kw_rows = {}, []

        for kr in kw_rows:
            kk = kr["keyword_key"]
            kw_text = kr.get("keyword") or kk
            vol = int(kr["search_volume"]) if kr.get("search_volume") is not None else 0
            cpc = float(kr["cpc"]) if kr.get("cpc") is not None else 0.0
            kd = float(kr["kd"]) if kr.get("kd") is not None else 0.0
            intents = list(kr.get("intents") or [])
            org_pos = own_org.get(kk, {}).get("position")
            monthly_org_value = round(vol * ctr(1) * cpc, 2) if (vol and cpc) else 0.0

            if cpc >= 1.0 and (org_pos is None or org_pos > 3):
                high_cpc_organic_targets.append({
                    "keyword": kw_text,
                    "volume": vol,
                    "cpc_usd": cpc,
                    "kd": kd,
                    "current_organic_position": org_pos,
                    "potential_monthly_ad_savings_usd": monthly_org_value,
                    "recommendation": "High CPC keyword: invest in SEO content & links to capture traffic organically without paying per click.",
                })
            if org_pos is not None and org_pos <= 3 and cpc > 0:
                saved = round(vol * ctr(org_pos) * cpc, 2)
                organic_saves_ppc_budget.append({
                    "keyword": kw_text,
                    "organic_position": org_pos,
                    "volume": vol,
                    "cpc_usd": cpc,
                    "monthly_organic_equivalent_value_usd": saved,
                    "recommendation": "Already ranking in Organic Top 3: reduce or pause non-brand PPC spend here to save budget.",
                })
            if (kd >= 45 or org_pos is None or org_pos > 10) and cpc > 0 and (set(intents) & {"transactional", "commercial"} or kk in by_kw):
                paid_bridges_high_kd_gap.append({
                    "keyword": kw_text,
                    "kd": kd,
                    "current_organic_position": org_pos,
                    "cpc_usd": cpc,
                    "volume": vol,
                    "recommendation": "High commercial intent with tough organic competition: run targeted Google Ads immediately while building organic authority.",
                })
            if org_pos is not None and org_pos <= 5 and kk in by_kw:
                dual_serp_domination.append({
                    "keyword": kw_text,
                    "organic_position": org_pos,
                    "cpc_usd": cpc,
                    "volume": vol,
                    "recommendation": "High-value competitive term: maintain both Organic Top 5 and Paid Top 3 to maximize total SERP real estate.",
                })

    return {
        "site_id": site.id,
        "own_domain": own_domain,
        "database": database,
        "domain_summaries": domain_summaries,
        "paid_keywords": [_ppc_out(r) for r in all_records[:150]],
        "paid_keyword_gap": {
            "missing_paid_keywords": paid_missing[:50],
            "shared_paid_keywords": paid_shared[:50],
        },
        "hybrid_strategy": {
            "high_cpc_organic_targets": high_cpc_organic_targets[:20],
            "organic_saves_ppc_budget": organic_saves_ppc_budget[:20],
            "paid_bridges_high_kd_gap": paid_bridges_high_kd_gap[:20],
            "dual_serp_domination": dual_serp_domination[:20],
        },
        "provenance": {
            "dataSource": "PPC_SERP_And_Keyword_Valuation_Engine",
            "data_source": "PPC_SERP_And_Keyword_Valuation_Engine",
            "source_class": "OBSERVED_AND_ESTIMATED",
            "collectedAt": now_iso,
            "collected_at": now_iso,
            "updatedAt": now_iso,
            "updated_at": now_iso,
            "isEstimated": True,
            "is_estimated": True,
            "confidenceScore": 0.72,
            "confidence_score": 0.72,
            "confidenceLevel": "MEDIUM",
            "confidence_level": "MEDIUM",
        },
    }
