"""Keyword Intelligence orchestration: credits preflight → provider runtime → ClickHouse → models.

Tenant first-party data (own GSC queries) is read from Postgres (RLS-scoped) and returned in a separate
`first_party` layer; it is never written to the global ClickHouse datasets.
"""

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, literal_column, select
from sqlalchemy.orm import Session

from app.infra.clickhouse import ClickHouse
from app.infra.storage import ObjectStore
from app.models import SearchPerformanceDaily, Site
from app.normalize.locale import SearchContext
from app.normalize.text import match_key, normalize_keyword
from app.providers import registry, runtime
from app.services import credits
from app.services.keywords import models, store


class ConfirmationRequired(Exception):
    def __init__(self, preflight: dict):
        super().__init__("confirmation required")
        self.preflight = preflight


def _archive():
    st = ObjectStore()
    return (lambda data: st.put("raw-provider", data, "json")) if st.configured else None


def _charge_or_preflight(db: Session, org_id: int, operation: str, qty: int, confirmed: bool, key: str) -> None:
    pf = credits.preflight(db, org_id, operation, qty)
    if not pf["allowed"]:
        raise credits.InsufficientCredits(f"needs {pf['estimated_credits']} credits, {pf['balance']} left")
    if not confirmed:
        raise ConfirmationRequired(pf)
    credits.charge(db, org_id, operation, qty, confirmed=True, idempotency_key=key)


async def lookup_metrics(db: Session, ch: ClickHouse, org_id: int, ctx: SearchContext, keywords: list[str],
                         confirmed: bool, request_id: str) -> dict:
    lang = ctx.locale.language
    kws = sorted({normalize_keyword(k, lang) for k in keywords if normalize_keyword(k, lang)})[:1000]
    provider = registry.get("keyword_metrics", db, org_id)
    _charge_or_preflight(db, org_id, "keyword_lookup", len(kws), confirmed, f"kw:{request_id}")
    res = await runtime.call(db, provider, "keyword_metrics", {"db": ctx.database, "kws": kws},
                             lambda: provider.keyword_metrics(ctx, kws), org_id=org_id, archive=_archive())
    from app.providers.base import KeywordMetric, Lineage
    metrics = [KeywordMetric(**{**m, "monthly": [tuple(x) for x in m["monthly"]], "lineage": Lineage(**m["lineage"])})
               for m in res["data"]]
    store.ingest_metrics(ch, metrics, res.get("raw_ref"))
    return {"ingested": len(metrics), "cached": res["cached"], "provider": res["provider"], "cost_usd": res["cost_usd"]}


async def fetch_serps(db: Session, ch: ClickHouse, org_id: int, ctx: SearchContext, keywords: list[str],
                      confirmed: bool, request_id: str) -> dict:
    lang = ctx.locale.language
    kws = sorted({normalize_keyword(k, lang) for k in keywords if normalize_keyword(k, lang)})[:500]
    provider = registry.get("serp", db, org_id)
    _charge_or_preflight(db, org_id, "serp_request", len(kws), confirmed, f"serp:{request_id}")
    from app.providers.base import Lineage, SerpItem, SerpResult
    done = []
    for kw in kws:
        res = await runtime.call(db, provider, "serp", {"db": ctx.database, "dev": ctx.device, "loc": ctx.location, "kw": kw,
                                                        "day": date.today().isoformat()},
                                 lambda kw=kw: provider.fetch_serp(ctx, kw), org_id=org_id, archive=_archive())
        d = res["data"]
        serp = SerpResult(d["keyword"], ctx, [SerpItem(**i) for i in d["items"]], d["serp_features"],
                          d["related_searches"], d["people_also_ask"], Lineage(**d["lineage"]))
        store.ingest_serp(ch, serp, raw_ref=res.get("raw_ref"))
        store.recompute_models(ch, kw, ctx.database)
        done.append(kw)
    return {"snapshots": len(done), "provider": provider.name}


def first_party_queries(db: Session, seed: str | None, lang: str, days: int = 90, limit: int = 200) -> dict:
    """Own GSC queries (RLS-scoped to the caller's org). FIRST_PARTY, never stored globally."""
    since = date.today() - timedelta(days=days)
    q = (select(SearchPerformanceDaily.query, func.sum(SearchPerformanceDaily.impressions).label("impr"),
                func.sum(SearchPerformanceDaily.clicks), func.sum(SearchPerformanceDaily.position * SearchPerformanceDaily.impressions))
         .where(SearchPerformanceDaily.date >= since).group_by(SearchPerformanceDaily.query))
    rows = db.execute(q).all()
    seed_key = match_key(seed, lang) if seed else None
    out = []
    for query, impr, clicks, wpos in rows:
        if seed_key and not all(t in match_key(query, lang).split() for t in seed_key.split()):
            continue
        out.append({"query": query, "impressions": int(impr), "clicks": int(clicks),
                    "avg_position": round(wpos / impr, 1) if impr else None, "source_class": "FIRST_PARTY"})
    out.sort(key=lambda r: (-r["impressions"], r["query"]))
    return {"source_class": "FIRST_PARTY", "window_days": days, "items": out[:limit], "total": len(out)}


def overview(db: Session, ch: ClickHouse, ctx: SearchContext, keyword: str) -> dict:
    lang = ctx.locale.language
    key = match_key(keyword, lang)
    metric = store.explorer(ch, ctx.database, q=keyword, match="exact", limit=1)["items"]
    serp = store.latest_serp(ch, key, ctx.database, ctx.device, ctx.location)
    similar = store.explorer(ch, ctx.database, q=keyword, match="broad", limit=20)["items"]
    questions = store.explorer(ch, ctx.database, q=keyword, match="broad", question=True, limit=20)["items"]
    kd = intent = None
    if serp["available"]:
        top10 = [r["domain"] for r in serp["organic"][:10]]
        kd = models.keyword_difficulty(top10, serp["features"])
        intent = models.classify_intent(keyword, lang, serp["features"])
    monthly_impr = _own_monthly_impressions(db, keyword, lang)
    return {"keyword": keyword, "database": ctx.database,
            "metrics": metric[0] if metric else None,
            "metrics_status": "available" if metric else "not_in_database (run a lookup; requires provider)",
            "trend": store.monthly_series(ch, key, ctx.database),
            "serp": serp, "kd": kd, "intent": intent or models.classify_intent(keyword, lang),
            "similar": [s for s in similar if s["keyword_key"] != key], "questions": questions,
            "first_party": {"gsc_volume_lower_bound": models.gsc_volume_lower_bound(monthly_impr),
                            "note": "Own GSC data; shown separately from provider metrics, never merged."}}


def _own_monthly_impressions(db: Session, keyword: str, lang: str) -> list[int]:
    key = match_key(keyword, lang)
    month = func.date_trunc(literal_column("'month'"), SearchPerformanceDaily.date).label("m")
    rows = db.execute(select(month, SearchPerformanceDaily.query, func.sum(SearchPerformanceDaily.impressions))
                      .group_by(literal_column("m"), SearchPerformanceDaily.query)).all()
    by_month: dict = defaultdict(int)
    for month, query, impr in rows:
        if match_key(query, lang) == key:
            by_month[month] += int(impr)
    return list(by_month.values())


def strategy(db: Session, ch: ClickHouse, ctx: SearchContext, site: Site, keywords: list[str], min_shared: int = 3) -> dict:
    """Topic map: SERP-overlap clusters (and lexical clusters separately) mapped to own URLs via own GSC data."""
    lang = ctx.locale.language
    serps, missing = {}, []
    for kw in keywords:
        s = store.latest_serp(ch, match_key(kw, lang), ctx.database, ctx.device, ctx.location)
        if s["available"]:
            serps[kw] = [r["url"] for r in s["organic"][:10]]
        else:
            missing.append(kw)
    clusters = models.serp_overlap_clusters(serps, min_shared)
    since = date.today() - timedelta(days=90)
    gsc = db.execute(select(SearchPerformanceDaily.query, SearchPerformanceDaily.page, func.sum(SearchPerformanceDaily.impressions))
                     .where(SearchPerformanceDaily.site_id == site.id, SearchPerformanceDaily.date >= since)
                     .group_by(SearchPerformanceDaily.query, SearchPerformanceDaily.page)).all()
    by_key: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for q, page, impr in gsc:
        by_key[match_key(q, lang)][page] += int(impr)
    out = []
    for c in clusters:
        pages: dict[str, int] = defaultdict(int)
        for kw in c.keywords:
            for page, impr in by_key.get(match_key(kw, lang), {}).items():
                pages[page] += impr
        total = sum(pages.values())
        ranked = sorted(pages.items(), key=lambda x: (-x[1], x[0]))
        cannibal = [p for p, i in ranked if total and i / total >= 0.2]
        out.append({"cluster_id": c.id, "keywords": c.keywords, "shared_top10_urls": c.shared_urls,
                    "pillar_keyword": c.keywords[0], "mapped_url": ranked[0][0] if ranked else None,
                    "action": "new_page_needed" if not ranked else ("resolve_cannibalization" if len(cannibal) > 1 else "optimize_existing"),
                    "cannibalization": cannibal if len(cannibal) > 1 else [],
                    "evidence": {"own_gsc_impressions_90d": total, "source": "gsc:own (FIRST_PARTY)" if total else None}})
    return {"database": ctx.database, "method": f"serp_overlap(min_shared={min_shared})", "clusters": out,
            "lexical_clusters": [{"cluster_id": c.id, "keywords": c.keywords} for c in models.lexical_clusters(keywords, lang)],
            "missing_serp": missing,
            "note": "Keywords without a SERP snapshot cannot be SERP-clustered; fetch SERPs first (credits)."}


def validate_kd_vs_rank(ch: ClickHouse, org_id: int, database: str) -> dict:
    """Validate KD-1 scores against observed rank tracking outcomes for the caller's tenant (§5)."""
    ranks = ch.tenant_query(
        "SELECT keyword_key, min(position) AS best_pos FROM rank_tracking_results FINAL "
        "WHERE tenant_id = {tenant_id:UInt64} AND locale = {db:String} AND position IS NOT NULL "
        "GROUP BY keyword_key",
        org_id,
        {"db": database},
    )
    if not ranks:
        return {"available": False, "reason": "no rank tracking data for this database yet", "model_version": models.KD_MODEL_VERSION}
    keys = [r["keyword_key"] for r in ranks]
    kds = {
        r["keyword_key"]: float(r["kd"])
        for r in ch.query(
            "SELECT keyword_key, kd FROM keyword_latest FINAL WHERE locale = {db:String} "
            "AND kd IS NOT NULL AND has({k:Array(String)}, keyword_key)",
            {"db": database, "k": keys},
        )
    }
    brackets = {"easy_0_29": [0, 0], "medium_30_59": [0, 0], "hard_60_100": [0, 0]}
    paired = 0
    for r in ranks:
        kd = kds.get(r["keyword_key"])
        if kd is None:
            continue
        paired += 1
        b = "easy_0_29" if kd < 30 else ("medium_30_59" if kd < 60 else "hard_60_100")
        brackets[b][1] += 1
        if int(r["best_pos"]) <= 10:
            brackets[b][0] += 1
    summary = {
        k: {"keywords": tot, "top10_count": hit, "top10_rate": round(hit / tot, 3) if tot else None}
        for k, (hit, tot) in brackets.items()
    }
    return {
        "available": paired > 0,
        "model_version": models.KD_MODEL_VERSION,
        "paired_keywords": paired,
        "brackets": summary,
        "note": "Observed top-10 attainment rate by KD bracket on this tenant's tracked keywords (correlation, not causation).",
    }


STOP_WORDS = {"ve", "ile", "için", "de", "da", "mi", "mu", "bir", "bu", "en", "in", "on", "for", "the", "of", "and", "to", "a"}


def _opportunity_score(vol: int | None, kd: float | None, comp: float | None) -> float | None:
    """Deterministic Low-hanging Fruit score (0-100): high search volume + low KD/competition."""
    if vol is None or vol <= 0:
        return None
    import math
    vol_factor = min(1.0, math.log10(1 + vol) / 5.0)
    difficulty = float(kd) if kd is not None else ((float(comp) * 100.0) if comp is not None else 45.0)
    ease_factor = max(0.05, (100.0 - min(100.0, difficulty)) / 100.0)
    return round(vol_factor * ease_factor * 100.0, 1)


def magic_tool(
    ch: ClickHouse,
    ctx: SearchContext,
    seed: str | None = None,
    *,
    match: str = "broad",
    intent: str | None = None,
    min_volume: int | None = None,
    max_volume: int | None = None,
    min_kd: float | None = None,
    max_kd: float | None = None,
    min_cpc: float | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    question: bool | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    group_token: str | None = None,
    limit: int = 200,
) -> dict:
    """Keyword Magic Tool (§4):
    - Discovers related keywords, questions, long-tail, variations, and intent-classified sets.
    - Groups keywords by topic/modifier root token with aggregate volume and average KD.
    - Identifies low-hanging fruit opportunities (high volume, low KD/competition).
    """
    from app.services.domains import make_provenance

    lang = ctx.locale.language
    country = ctx.database.split(":")[0].split("-")[-1].upper() if "-" in ctx.database else ctx.database[:2].upper()
    raw = store.explorer(
        ch,
        ctx.database,
        q=seed if seed else None,
        match=match,
        min_volume=min_volume,
        max_volume=max_volume,
        min_kd=min_kd,
        max_kd=max_kd,
        min_cpc=min_cpc,
        intent=intent,
        min_words=min_words,
        max_words=max_words,
        question=question,
        include=include or [],
        exclude=exclude or [],
        limit=max(limit, 500),
    )["items"]

    # If broad match on seed yielded few items, also pull broader corpus for semantic/related discovery
    all_corpus = raw
    if seed and len(raw) < 20:
        extra = store.explorer(ch, ctx.database, limit=300)["items"]
        seen_k = {r["keyword_key"] for r in raw}
        for ex in extra:
            if ex["keyword_key"] not in seen_k:
                all_corpus.append(ex)
                seen_k.add(ex["keyword_key"])

    seed_key = match_key(seed, lang) if seed else ""
    seed_tokens = set(seed_key.split())
    seed_lex = models._lex_key(seed, lang) if seed else ""

    enriched: list[dict] = []
    topic_groups: dict[str, list[dict]] = defaultdict(list)

    for r in all_corpus:
        kw = r["keyword"]
        kkey = r["keyword_key"]
        vol = int(r["volume"]) if r.get("volume") is not None else None
        kd_val = round(float(r["kd"]), 1) if r.get("kd") is not None else None
        cpc_val = round(float(r["cpc"]), 2) if r.get("cpc") is not None else None
        comp_val = round(float(r["competition"]), 2) if r.get("competition") is not None else None
        intents = list(r.get("intents") or [])
        if not intents:
            intents = models.classify_intent(kw, lang, list(r.get("features") or []))["intents"]
        opp_score = _opportunity_score(vol, kd_val, comp_val)
        is_low_hanging = bool(
            vol is not None
            and vol >= 50
            and ((kd_val is not None and kd_val <= 40.0) or (kd_val is None and (comp_val is None or comp_val <= 0.5)))
        )

        tokens = [t for t in kkey.split() if len(t) > 2 and t not in STOP_WORDS and t not in seed_tokens]
        if not tokens:
            tokens = [t for t in kkey.split() if len(t) > 2] or ["general"]

        if group_token and match_key(group_token, lang) not in kkey.split():
            continue

        item = {
            **r,
            "volume": vol,
            "kd": kd_val,
            "cpc": cpc_val,
            "competition": comp_val,
            "intents": intents,
            "country": country,
            "device": ctx.device,
            "opportunity_score": opp_score,
            "is_low_hanging_fruit": is_low_hanging,
            "provenance": make_provenance(
                r.get("provider") or "Keyword_Database",
                "DEMO" if r.get("is_synthetic") else (r.get("source_class") or "PROVIDER"),
                str(r.get("fetched_at")) if r.get("fetched_at") else None,
                str(r.get("fetched_at")) if r.get("fetched_at") else None,
                bool(r.get("is_synthetic")),
                0.85 if not r.get("is_synthetic") else 0.5,
            ),
        }
        enriched.append(item)
        for tok in set(tokens[:3]):
            topic_groups[tok].append(item)

    groups_out = []
    for tok, g_items in topic_groups.items():
        vols = [x["volume"] for x in g_items if x["volume"] is not None]
        kds = [x["kd"] for x in g_items if x["kd"] is not None]
        cpcs = [x["cpc"] for x in g_items if x["cpc"] is not None]
        groups_out.append({
            "topic": tok,
            "group": tok,
            "keyword_count": len(g_items),
            "count": len(g_items),
            "total_volume": sum(vols) if vols else None,
            "avg_kd": round(sum(kds) / len(kds), 1) if kds else None,
            "avg_cpc": round(sum(cpcs) / len(cpcs), 2) if cpcs else None,
            "sample_keywords": [x["keyword"] for x in g_items[:5]],
            "keywords": [x["keyword"] for x in g_items[:5]],
        })
    groups_out.sort(key=lambda g: (-(g["total_volume"] or 0), -g["keyword_count"], g["topic"]))

    # Discovery categorizations (§4)
    suggestions = [x for x in enriched if not seed_key or seed_key in x["keyword_key"]][:limit]
    related = [x for x in enriched if seed_key and seed_key not in x["keyword_key"]][:limit]
    long_tail = [x for x in enriched if int(x.get("words") or len(x["keyword"].split())) >= 3][:limit]
    questions = [x for x in enriched if bool(x.get("question"))][:limit]
    variations = [x for x in enriched if seed_lex and models._lex_key(x["keyword"], lang) == seed_lex][:limit]
    low_hanging = sorted(
        [x for x in enriched if x["is_low_hanging_fruit"]],
        key=lambda x: -(x["opportunity_score"] or 0),
    )[:limit]

    by_intent = {
        "commercial": [x for x in enriched if "commercial" in x["intents"]][:limit],
        "informational": [x for x in enriched if "informational" in x["intents"]][:limit],
        "transactional": [x for x in enriched if "transactional" in x["intents"]][:limit],
        "navigational": [x for x in enriched if "navigational" in x["intents"]][:limit],
    }

    total_vol = sum(x["volume"] for x in enriched if x["volume"] is not None)
    all_kds = [x["kd"] for x in enriched if x["kd"] is not None]

    return {
        "seed": seed or "",
        "database": ctx.database,
        "country": country,
        "device": ctx.device,
        "total": len(enriched),
        "aggregate_metrics": {
            "total_volume": total_vol if any(x["volume"] is not None for x in enriched) else None,
            "avg_kd": round(sum(all_kds) / len(all_kds), 1) if all_kds else None,
            "low_hanging_count": len(low_hanging),
            "question_count": len(questions),
            "long_tail_count": len(long_tail),
        },
        "topic_groups": groups_out[:30],
        "items": enriched[:limit],
        "long_tail": long_tail[:50],
        "questions": questions[:50],
        "low_hanging_fruit": low_hanging[:50],
        "intent_groups": {k: len(v) for k, v in by_intent.items()},
        "discovery": {
            "suggestions": suggestions[:50],
            "related": related[:50],
            "long_tail": long_tail[:50],
            "questions": questions[:50],
            "variations": variations[:50],
            "low_hanging_fruit": low_hanging[:50],
            "by_intent": {k: v[:50] for k, v in by_intent.items()},
        },
        "provenance": make_provenance("Keyword_Magic_Engine", "MODELED", None, None, True, 0.8),
    }


def compare_keywords(ch: ClickHouse, ctx: SearchContext, keywords: list[str]) -> dict:
    """Side-by-side comparison of 2–20 keywords across metrics, trend, intent, SERP features, and opportunity score."""
    from app.services.domains import make_provenance

    lang = ctx.locale.language
    country = ctx.database.split(":")[0].split("-")[-1].upper() if "-" in ctx.database else ctx.database[:2].upper()
    items = []
    domain_counts: dict[str, int] = defaultdict(int)
    for kw in keywords[:20]:
        norm = normalize_keyword(kw, lang)
        if not norm:
            continue
        key = match_key(norm, lang)
        ex = store.explorer(ch, ctx.database, q=norm, match="exact", limit=1)["items"]
        row = ex[0] if ex else None
        serp = store.latest_serp(ch, key, ctx.database, ctx.device, ctx.location)
        trend = store.monthly_series(ch, key, ctx.database)
        vol = int(row["volume"]) if row and row.get("volume") is not None else None
        kd_val = round(float(row["kd"]), 1) if row and row.get("kd") is not None else None
        cpc_val = round(float(row["cpc"]), 2) if row and row.get("cpc") is not None else None
        comp_val = round(float(row["competition"]), 2) if row and row.get("competition") is not None else None
        intents = list(row["intents"]) if row and row.get("intents") else models.classify_intent(norm, lang, serp.get("features") if serp.get("available") else None)["intents"]
        features = list(row["features"]) if row and row.get("features") else (serp.get("features", []) if serp.get("available") else [])
        top_domains = [r["domain"] for r in serp.get("organic", [])[:5]] if serp.get("available") else []
        for d in set(top_domains):
            domain_counts[d] += 1
        items.append({
            "keyword": norm,
            "keyword_key": key,
            "volume": vol,
            "kd": kd_val,
            "cpc": cpc_val,
            "competition": comp_val,
            "intents": intents,
            "serp_features": features,
            "word_count": len(norm.split()),
            "country": country,
            "device": ctx.device,
            "opportunity_score": _opportunity_score(vol, kd_val, comp_val),
            "trend": trend,
            "top_serp_domains": top_domains,
            "top_domains": top_domains,
            "source_class": ("DEMO" if row.get("is_synthetic") else row.get("source_class", "PROVIDER")) if row else "MODELED",
            "provenance": make_provenance(
                row.get("provider") if row else "Keyword_Comparison",
                ("DEMO" if row.get("is_synthetic") else row.get("source_class", "PROVIDER")) if row else "MODELED",
                str(row.get("fetched_at")) if row and row.get("fetched_at") else None,
                None,
                bool(row and row.get("is_synthetic")),
                0.85 if row else 0.4,
            ),
        })
    shared_domains = [{"domain": d, "shared_keywords": c} for d, c in sorted(domain_counts.items(), key=lambda x: (-x[1], x[0])) if c >= 2]
    return {"database": ctx.database, "country": country, "device": ctx.device, "items": items, "shared_serp_domains": shared_domains}


def keyword_history(db: Session, ch: ClickHouse, org_id: int, database: str, keyword: str | None = None, limit: int = 100) -> dict:
    """Return historical metric snapshots, recent audit searches, and recent keywords from ClickHouse."""
    from app.models import AuditLog

    lang = database.split("-")[0]
    logs = db.scalars(
        select(AuditLog)
        .where(AuditLog.org_id == org_id, AuditLog.action.like("keywords.%"))
        .order_by(AuditLog.id.desc())
        .limit(20)
    ).all()
    recent_searches = [
        {"id": lg.id, "action": lg.action, "metadata": lg.details or {}, "created_at": lg.created_at.isoformat()}
        for lg in logs
    ]
    if keyword:
        key = match_key(keyword, lang)
        rows = ch.query(
            "SELECT keyword, keyword_key, month, argMax(search_volume, version) AS volume, argMax(cpc, version) AS cpc, "
            "argMax(ads_competition, version) AS competition, argMax(kd, version) AS kd, max(fetched_at) AS fetched_at, "
            "any(provider) AS provider, any(source_class) AS source_class "
            "FROM keyword_metrics WHERE locale = {db:String} AND keyword_key = {k:String} "
            "GROUP BY keyword, keyword_key, month ORDER BY month ASC",
            {"db": database, "k": key},
        )
        return {"database": database, "keyword": keyword, "items": rows, "recent_keywords": rows, "recent_searches": recent_searches}

    rows = ch.query(
        "SELECT keyword, keyword_key, max(month) AS latest_month, argMax(search_volume, version) AS volume, "
        "argMax(cpc, version) AS cpc, argMax(kd, version) AS kd, max(fetched_at) AS fetched_at, "
        "any(provider) AS provider, any(source_class) AS source_class "
        "FROM keyword_latest FINAL WHERE locale = {db:String} "
        "GROUP BY keyword, keyword_key ORDER BY fetched_at DESC, keyword_key ASC LIMIT {lim:UInt32}",
        {"db": database, "lim": limit},
    )
    return {"database": database, "items": rows, "recent_keywords": rows, "recent_searches": recent_searches}


def format_keywords_csv(rows: list[dict]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["keyword", "volume", "kd", "cpc", "competition", "intents", "serp_features", "word_count", "opportunity_score", "source_class"])
    for r in rows:
        writer.writerow([
            r.get("keyword", ""),
            r.get("volume") if r.get("volume") is not None else "",
            r.get("kd") if r.get("kd") is not None else "",
            r.get("cpc") if r.get("cpc") is not None else "",
            r.get("competition") if r.get("competition") is not None else "",
            "|".join(r.get("intents") or []),
            "|".join(r.get("features") or r.get("serp_features") or []),
            r.get("words") or r.get("word_count") or "",
            r.get("opportunity_score") if r.get("opportunity_score") is not None else "",
            r.get("source_class", ""),
        ])
    return buf.getvalue()
