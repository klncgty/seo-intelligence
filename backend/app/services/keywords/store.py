"""ClickHouse access for global keyword + SERP datasets (no tenant data here)."""

import time
from datetime import UTC, date, datetime

from app.infra.clickhouse import ClickHouse
from app.normalize.domains import registrable_domain
from app.normalize.text import is_question, match_key
from app.providers.base import KeywordMetric, SerpResult
from app.services.keywords import models

SORTS = {"volume": "volume DESC", "kd": "kd ASC", "cpc": "cpc DESC", "keyword": "keyword ASC"}


def _version() -> int:
    return time.time_ns()


def _lang(database: str) -> str:
    return database.split("-")[0]


def ingest_metrics(ch: ClickHouse, metrics: list[KeywordMetric], raw_ref: str | None = None) -> int:
    rows = []
    v = _version()
    for m in metrics:
        lang = _lang(m.database)
        points = [(f"{mon}-01", vol) for mon, vol in m.monthly] or [(date.today().replace(day=1).isoformat(), m.search_volume)]
        latest_month = max(p[0] for p in points)
        for month, vol in points:
            vol = m.search_volume if month == latest_month and m.search_volume is not None else vol
            rows.append({
                "keyword": m.keyword, "keyword_key": match_key(m.keyword, lang), "locale": m.database, "month": month,
                "search_volume": vol, "volume_precision": m.volume_precision, "cpc": m.cpc, "cpc_currency": m.cpc_currency or "",
                "ads_competition": m.ads_competition, "kd": None, "kd_model_version": "", "intents": [], "serp_features": [],
                "word_count": len(m.keyword.split()), "is_question": int(is_question(m.keyword, lang)), "cluster_id": "",
                "source_class": m.lineage.source_class, "provider": m.lineage.provider,
                "provider_request_id": m.lineage.provider_request_id or "", "raw_ref": raw_ref or m.lineage.raw_ref or "",
                "cost_units": m.lineage.cost_usd, "fetched_at": _ts(m.lineage.fetched_at), "is_synthetic": int(m.lineage.is_synthetic),
                "version": v})
    ch.insert("keyword_metrics", rows)
    _write_latest(ch, rows)
    return len(rows)


LATEST_COLS = ("keyword", "keyword_key", "locale", "month", "search_volume", "volume_precision", "cpc", "cpc_currency",
               "ads_competition", "kd", "kd_model_version", "intents", "serp_features", "word_count", "is_question",
               "source_class", "provider", "fetched_at", "is_synthetic", "version")


def _write_latest(ch: ClickHouse, rows: list[dict]) -> None:
    latest: dict[tuple, dict] = {}
    for r in rows:
        k = (r["locale"], r["keyword_key"])
        if k not in latest or str(r["month"]) >= str(latest[k]["month"]):
            latest[k] = r
    ch.insert("keyword_latest", [{c: r.get(c) for c in LATEST_COLS} for r in latest.values()])


def _ts(dt: datetime | str | None) -> str:
    if isinstance(dt, str):  # lineage round-tripped through the JSON cache
        dt = datetime.fromisoformat(dt)
    return (dt or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def ingest_serp(ch: ClickHouse, serp: SerpResult, snapshot_date: date | None = None, raw_ref: str | None = None) -> int:
    lang = _lang(serp.context.database)
    v = _version()
    rows = [{
        "keyword_key": match_key(serp.keyword, lang), "keyword": serp.keyword, "locale": serp.context.database,
        "device": serp.context.device, "location": serp.context.location, "snapshot_date": (snapshot_date or date.today()).isoformat(),
        "rank_group": it.rank_group, "rank_absolute": it.rank_absolute, "result_type": it.result_type, "url": it.url or "",
        "domain": it.domain or "", "registrable_domain": registrable_domain(it.domain or "") or (it.domain or ""),
        "title": it.title or "", "serp_features": serp.serp_features, "ai_overview_cited": int(bool(it.cited_domains)),
        "source_class": serp.lineage.source_class, "provider": serp.lineage.provider,
        "provider_request_id": serp.lineage.provider_request_id or "", "raw_ref": raw_ref or "",
        "fetched_at": _ts(serp.lineage.fetched_at), "is_synthetic": int(serp.lineage.is_synthetic), "version": v,
    } for it in serp.items]
    return ch.insert("serp_results", rows)


def latest_serp(ch: ClickHouse, keyword_key: str, database: str, device: str = "desktop", location: str = "") -> dict:
    rows = ch.query(
        "SELECT snapshot_date, rank_group, rank_absolute, result_type, url, registrable_domain AS domain, title, serp_features, "
        "provider, source_class, fetched_at, is_synthetic FROM serp_results FINAL "
        "WHERE keyword_key = {k:String} AND locale = {db:String} AND device = {dev:String} AND location = {loc:String} "
        "AND snapshot_date = (SELECT max(snapshot_date) FROM serp_results WHERE keyword_key = {k:String} AND locale = {db:String} "
        "AND device = {dev:String} AND location = {loc:String}) ORDER BY rank_absolute",
        {"k": keyword_key, "db": database, "dev": device, "loc": location})
    if not rows:
        return {"available": False}
    organic = [r for r in rows if r["result_type"] == "organic"]
    return {"available": True, "snapshot_date": rows[0]["snapshot_date"], "features": rows[0]["serp_features"],
            "provider": rows[0]["provider"], "source_class": rows[0]["source_class"], "fetched_at": rows[0]["fetched_at"],
            "is_synthetic": bool(int(rows[0]["is_synthetic"])), "organic": organic[:100], "all": rows}


def recompute_models(ch: ClickHouse, keyword: str, database: str, domain_strength: dict | None = None,
                     brand_terms: list[str] | None = None) -> dict | None:
    """Recompute intent + KD from the latest SERP snapshot and write a new version of the latest metric row."""
    lang = _lang(database)
    key = match_key(keyword, lang)
    serp = latest_serp(ch, key, database)
    if not serp["available"]:
        return None
    top10 = [r["domain"] for r in serp["organic"][:10]]
    if domain_strength is None:  # authority component from our backlink-index domain strength (ds-1), when available
        from app.services.backlinks import strengths
        try:
            domain_strength = strengths(ch, top10)
        except Exception:
            domain_strength = None
    kd = models.keyword_difficulty(top10, serp["features"], domain_strength, keyword, lang)
    intent = models.classify_intent(keyword, lang, serp["features"], brand_terms)
    latest = ch.query("SELECT * FROM keyword_metrics FINAL WHERE locale = {db:String} AND keyword_key = {k:String} "
                      "ORDER BY month DESC LIMIT 1", {"db": database, "k": key})
    base = latest[0] if latest else {
        "keyword": keyword, "keyword_key": key, "locale": database, "month": date.today().replace(day=1).isoformat(),
        "search_volume": None, "volume_precision": "unknown", "cpc": None, "cpc_currency": "", "ads_competition": None,
        "word_count": len(keyword.split()), "is_question": int(is_question(keyword, lang)), "cluster_id": "",
        "source_class": serp["source_class"], "provider": serp["provider"], "provider_request_id": "", "raw_ref": "",
        "cost_units": 0, "fetched_at": _ts(None), "is_synthetic": int(serp["is_synthetic"])}
    row = {**base, "kd": kd["kd"], "kd_model_version": kd["model_version"], "intents": intent["intents"],
           "serp_features": serp["features"], "version": _version()}
    ch.insert("keyword_metrics", [row])
    _write_latest(ch, [row])
    return {"kd": kd, "intent": intent}


def explorer(ch: ClickHouse, database: str, *, q: str | None = None, match: str = "broad", min_volume: int | None = None,
             max_volume: int | None = None, min_kd: float | None = None, max_kd: float | None = None,
             min_cpc: float | None = None, intent: str | None = None, feature: str | None = None,
             min_words: int | None = None, max_words: int | None = None, question: bool | None = None,
             include: list[str] | None = None, exclude: list[str] | None = None, sort: str = "volume",
             limit: int = 50, offset: int = 0) -> dict:
    lang = _lang(database)
    where, params = ["locale = {db:String}"], {"db": database}
    if q:
        qk = match_key(q, lang)
        params["q"] = qk
        if match == "exact":
            where.append("keyword_key = {q:String}")
        elif match == "phrase":
            where.append("position(keyword_key, {q:String}) > 0")
        else:
            for i, tok in enumerate(qk.split()[:8]):
                params[f"t{i}"] = tok
                where.append(f"hasToken(keyword_key, {{t{i}:String}})")
    for i, inc in enumerate(include or []):
        params[f"inc{i}"] = match_key(inc, lang)
        where.append(f"position(keyword_key, {{inc{i}:String}}) > 0")
    for i, exc in enumerate(exclude or []):
        params[f"exc{i}"] = match_key(exc, lang)
        where.append(f"position(keyword_key, {{exc{i}:String}}) = 0")
    having = []
    for cond, val, name, typ in [("volume >= {minv:UInt64}", min_volume, "minv", int), ("volume <= {maxv:UInt64}", max_volume, "maxv", int),
                                 ("kd >= {minkd:Float64}", min_kd, "minkd", float), ("kd <= {maxkd:Float64}", max_kd, "maxkd", float),
                                 ("cpc >= {mincpc:Float64}", min_cpc, "mincpc", float), ("words >= {minw:UInt8}", min_words, "minw", int),
                                 ("words <= {maxw:UInt8}", max_words, "maxw", int)]:
        if val is not None:
            having.append(cond)
            params[name] = typ(val)
    if intent:
        having.append("has(intents, {intent:String})")
        params["intent"] = intent
    if feature:
        having.append("has(features, {feature:String})")
        params["feature"] = feature
    if question is not None:
        having.append(f"question = {1 if question else 0}")
    cols = ("keyword_key, keyword, search_volume AS volume, volume_precision, cpc, cpc_currency, ads_competition AS competition, kd, "
            "kd_model_version AS kd_model, intents, serp_features AS features, word_count AS words, is_question AS question, "
            "provider, source_class, fetched_at, is_synthetic")
    having = [h.replace("volume", "search_volume").replace("words", "word_count").replace("features", "serp_features")
              .replace("question =", "is_question =") for h in having]
    sql = f"SELECT {cols} FROM keyword_latest FINAL WHERE {' AND '.join(where + having)}"  # nosec B608 - fixed fragments + params
    order = SORTS.get(sort, SORTS["volume"])
    # nosec B608: `sql` is assembled only from fixed fragments with {name:Type} placeholders; values travel as params
    total = ch.query(f"SELECT count() AS n FROM ({sql})", params)[0]["n"]  # nosec B608
    rows = ch.query(f"{sql} ORDER BY {order} NULLS LAST, keyword_key LIMIT {int(limit)} OFFSET {int(offset)}", params)  # nosec B608
    return {"total": int(total), "items": rows}


def monthly_series(ch: ClickHouse, keyword_key: str, database: str) -> list[dict]:
    return ch.query("SELECT month, argMax(search_volume, version) AS volume FROM keyword_metrics WHERE locale = {db:String} "
                    "AND keyword_key = {k:String} GROUP BY month ORDER BY month", {"db": database, "k": keyword_key})

