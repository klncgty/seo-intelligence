"""Backlink intelligence (spec §8) on the ClickHouse `backlinks` index.

Sources (column `source`): own_crawl | common_crawl | provider:<name>. Every response carries a coverage
statement: our index is partial — "X links found in our index; the real number may be higher".

Domain strength `ds-1`: PageRank (damping 0.85) on the referring-domain graph (one edge per source→target
domain pair; nofollow/ugc/sponsored weight 0.1), spam-penalized, mapped to 0–100 on a log scale.
"""

import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime

from app.infra.clickhouse import ClickHouse
from app.normalize.domains import registrable_domain
from app.normalize.text import match_key

DS_MODEL_VERSION = "ds-1"
GENERIC_ANCHORS = {"buraya", "tıklayın", "tikla", "tıkla", "burada", "kaynak", "link", "site", "web sitesi", "detaylar",
                   "devamı", "devamini oku", "click here", "here", "website", "this", "read more", "source", "more", "visit"}


def _v() -> int:
    import time
    return time.time_ns()


def _dom(url: str) -> str:
    """Registrable domain; falls back to the bare host for IPs / single-label hosts."""
    host = (url.split("/") + ["", "", ""])[2].split(":")[0].lower()
    return registrable_domain(host) or host


def ingest(ch: ClickHouse, links: list[dict], source: str, source_class: str, is_synthetic: bool = False) -> int:
    """links: {source_url, target_url, anchor, rel, link_position, is_image, first_seen?, last_seen?, status?}"""
    today = date.today().isoformat()
    v = _v()
    rows = []
    for link in links:
        sd, td = _dom(link["source_url"]), _dom(link["target_url"])
        if not sd or not td or sd == td:
            continue
        rows.append({"source_url": link["source_url"][:2000], "source_domain": sd, "target_url": link["target_url"][:2000],
                     "target_domain": td, "anchor": (link.get("anchor") or "")[:500], "rel": (link.get("rel") or "")[:50],
                     "link_position": link.get("link_position") or "unknown", "is_image": int(bool(link.get("is_image"))),
                     "first_seen": link.get("first_seen") or today, "last_seen": link.get("last_seen") or today,
                     "last_checked": link.get("last_checked") or today, "status": link.get("status") or "live",
                     "source": source, "source_class": source_class, "fetched_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                     "is_synthetic": int(is_synthetic), "version": v})
    return ch.insert("backlinks", rows)


def coverage(ch: ClickHouse, domain: str) -> dict:
    r = ch.query("SELECT count() AS links, uniqExact(source_domain) AS rd, min(first_seen) AS first, max(last_checked) AS last, "
                 "groupUniqArray(source) AS sources FROM backlinks FINAL WHERE target_domain = {d:String}", {"d": domain})[0]
    total = ch.query("SELECT count() AS n FROM backlinks FINAL")[0]["n"]
    return {"links_in_index": int(r["links"]), "referring_domains_in_index": int(r["rd"]), "index_size": int(total),
            "date_range": {"first_seen": str(r["first"]) if int(r["links"]) else None, "last_checked": str(r["last"]) if int(r["links"]) else None},
            "sources": r["sources"], "statement": f"{int(r['links'])} links found in our index; the real number may be higher."}


def classify_anchor(anchor: str, target_domain: str, keywords: list[str] | None = None) -> str:
    a = match_key(anchor or "")
    if not a:
        return "image_or_empty"
    label = match_key(target_domain.split(".")[0])
    if re.match(r"^(https?://|www\.)", a) or a.replace("www.", "").startswith(target_domain):
        return "url"
    kws = [match_key(k) for k in keywords or []]
    if kws and a in kws:
        return "exact"
    if label and label in a.replace(" ", ""):
        return "brand"
    if a in {match_key(g) for g in GENERIC_ANCHORS}:
        return "generic"
    if kws and any(set(k.split()) & set(a.split()) for k in kws):
        return "partial"
    return "other"


TLD_TO_COUNTRY = {
    "tr": "TR", "uk": "GB", "de": "DE", "fr": "FR", "nl": "NL", "es": "ES",
    "it": "IT", "ru": "RU", "br": "BR", "in": "IN", "jp": "JP", "au": "AU",
    "ca": "CA", "us": "US", "pl": "PL", "se": "SE", "ch": "CH", "at": "AT",
}
SPAMMY_TLDS = {"xyz", "top", "work", "click", "loan", "gq", "cf", "tk", "ml", "ga", "biz"}
SPAMMY_TOKENS = {"casino", "bet", "poker", "slots", "viagra", "cialis", "payday", "loan", "porn", "xxx", " directory", "free-links", "linkfarm"}


def overview(ch: ClickHouse, domain: str, keywords: list[str] | None = None) -> dict:
    d = registrable_domain(domain) or domain
    p = {"d": d}
    now_iso = datetime.now(UTC).isoformat()
    totals = ch.query(
        "SELECT countIf(status = 'live') AS live, countIf(status = 'lost') AS lost, uniqExactIf(source_domain, status='live') AS rd, "
        "countIf(status = 'live' AND (rel = '' OR rel = 'follow')) AS follow, "
        "countIf(rel != '' AND rel != 'follow') AS nofollow, "
        "countIf(position(lower(rel), 'ugc') > 0) AS ugc, "
        "countIf(position(lower(rel), 'sponsored') > 0) AS sponsored, "
        "countIf(is_image = 1) AS images, "
        "countIf(is_image = 0 AND status = 'live') AS text_links "
        "FROM backlinks FINAL WHERE target_domain = {d:String}",
        p,
    )[0]
    trend = ch.query(
        "SELECT toStartOfMonth(first_seen) AS month, count() AS new_links, "
        "countIf(status = 'lost') AS lost_links, uniqExact(source_domain) AS new_rd "
        "FROM backlinks FINAL WHERE target_domain = {d:String} GROUP BY month ORDER BY month",
        p,
    )
    anchors = ch.query("SELECT anchor, count() AS n FROM backlinks FINAL WHERE target_domain = {d:String} AND status = 'live' "
                       "GROUP BY anchor ORDER BY n DESC LIMIT 500", p)
    dist = Counter()
    for a in anchors:
        dist[classify_anchor(a["anchor"], d, keywords)] += int(a["n"])
    tlds = ch.query("SELECT arrayElement(splitByChar('.', source_domain), -1) AS tld, uniqExact(source_domain) AS rd "
                    "FROM backlinks FINAL WHERE target_domain = {d:String} AND status = 'live' GROUP BY tld ORDER BY rd DESC LIMIT 15", p)
    country_counts: Counter = Counter()
    for t in tlds:
        cc = TLD_TO_COUNTRY.get(str(t["tld"]).lower(), "GLOBAL")
        country_counts[cc] += int(t["rd"])
    country_distribution = [{"country": k, "referring_domains": v} for k, v in country_counts.most_common()]
    pages_raw = ch.query("SELECT target_url, uniqExact(source_domain) AS rd, count() AS links FROM backlinks FINAL "
                         "WHERE target_domain = {d:String} AND status = 'live' GROUP BY target_url ORDER BY rd DESC LIMIT 20", p)
    pages = [
        {
            "target_url": r["target_url"],
            "url": r["target_url"],
            "rd": int(r["rd"]),
            "referring_domains": int(r["rd"]),
            "links": int(r["links"]),
            "backlinks": int(r["links"]),
        }
        for r in pages_raw
    ]
    positions = ch.query("SELECT link_position, count() AS n FROM backlinks FINAL WHERE target_domain = {d:String} GROUP BY link_position", p)
    pos_map = {r["link_position"]: int(r["n"]) for r in positions}
    strength = ch.query("SELECT argMax(score, version) AS score, argMax(model_version, version) AS mv FROM domain_strength "
                        "WHERE registrable_domain = {d:String}", p)
    cov = coverage(ch, d)
    live_n = int(totals["live"])
    lost_n = int(totals["lost"])
    nofollow_n = int(totals["nofollow"])
    nofollow_ratio = round(nofollow_n / live_n, 3) if live_n > 0 else None
    anchor_dist = dict(dist)
    anchor_types_detailed = {
        "branded": anchor_dist.get("brand", 0),
        "exact_match": anchor_dist.get("exact", 0),
        "partial_match": anchor_dist.get("partial", 0),
        "naked_url": anchor_dist.get("url", 0),
        "generic": anchor_dist.get("generic", 0),
        "image_or_empty": anchor_dist.get("image_or_empty", 0),
        "other": anchor_dist.get("other", 0),
    }
    return {
        "domain": d,
        "source_class": "OBSERVED/PROVIDER (see coverage.sources)",
        "coverage": {
            **cov,
            "index_total_links": cov["index_size"],
            "first_seen": cov["date_range"]["first_seen"],
            "last_seen": cov["date_range"]["last_checked"],
        },
        "total_backlinks": live_n + lost_n,
        "live_links": live_n,
        "live_backlinks": live_n,
        "lost_links": lost_n,
        "referring_domains": int(totals["rd"]),
        "follow_links": int(totals["follow"]),
        "nofollow_links": nofollow_n,
        "nofollow_ratio": nofollow_ratio,
        "ugc_links": int(totals["ugc"]),
        "sponsored_links": int(totals["sponsored"]),
        "image_links": int(totals["images"]),
        "link_types": {
            "text": int(totals["text_links"]),
            "image": int(totals["images"]),
            "redirect": pos_map.get("redirect", 0),
            "form": pos_map.get("form", 0),
            "frame": pos_map.get("frame", 0),
        },
        "referring_ips_and_subnets": {
            "unique_referring_hosts": int(totals["rd"]),
            "c_class_subnets": None,
            "status": "HOST_LEVEL_INDEX",
            "note": "Referring domains are deduplicated at registrable domain level; C-class IP subnet resolution requires live DNS enrichment.",
        },
        "domain_strength": {
            "score": strength[0]["score"] if strength and strength[0]["mv"] else None,
            "model_version": strength[0]["mv"] or None if strength else None,
            "source_class": "MODELED",
        },
        "new_by_month": trend,
        "anchor_distribution": anchor_dist,
        "anchor_types": anchor_types_detailed,
        "anchors": anchor_dist,
        "top_anchors": anchors[:20],
        "tld_distribution": tlds,
        "top_tlds": [{"tld": r["tld"], "domains": int(r["rd"])} for r in tlds],
        "country_distribution": country_distribution,
        "top_linked_pages": pages,
        "top_pages": pages,
        "link_positions": pos_map,
        "provenance": {
            "dataSource": ",".join(cov["sources"]) if cov["sources"] else "ClickHouse_Backlink_Index",
            "data_source": ",".join(cov["sources"]) if cov["sources"] else "ClickHouse_Backlink_Index",
            "source_class": "OBSERVED",
            "collectedAt": cov["date_range"]["last_checked"] or now_iso,
            "collected_at": cov["date_range"]["last_checked"] or now_iso,
            "updatedAt": now_iso,
            "updated_at": now_iso,
            "isEstimated": False,
            "is_estimated": False,
            "confidenceScore": 0.85 if live_n > 0 else 0.4,
            "confidence_score": 0.85 if live_n > 0 else 0.4,
            "confidenceLevel": "HIGH" if live_n > 0 else "LOW",
            "confidence_level": "HIGH" if live_n > 0 else "LOW",
        },
    }


def new_lost(ch: ClickHouse, domain: str, days: int = 30) -> dict:
    p = {"d": registrable_domain(domain) or domain, "days": days}
    new = ch.query("SELECT source_url, target_url, anchor, first_seen FROM backlinks FINAL WHERE target_domain = {d:String} "
                   "AND first_seen >= today() - {days:UInt32} ORDER BY first_seen DESC LIMIT 500", p)
    lost = ch.query("SELECT source_url, target_url, anchor, last_seen FROM backlinks FINAL WHERE target_domain = {d:String} "
                    "AND status = 'lost' AND last_checked >= today() - {days:UInt32} ORDER BY last_seen DESC LIMIT 500", p)
    return {"window_days": days, "new": new, "lost": lost}


def referring_domains(ch: ClickHouse, domain: str, limit: int = 200) -> list[dict]:
    rows = ch.query(
        "SELECT b.source_domain AS domain, count() AS links, min(b.first_seen) AS first_seen, max(b.last_seen) AS last_seen, "
        "any(b.anchor) AS sample_anchor, groupUniqArray(b.source) AS sources, "
        "any(s.score) AS strength FROM backlinks AS b FINAL LEFT JOIN (SELECT registrable_domain, argMax(score, version) AS score "
        "FROM domain_strength GROUP BY registrable_domain) AS s ON s.registrable_domain = b.source_domain "
        "WHERE b.target_domain = {d:String} AND b.status = 'live' GROUP BY domain ORDER BY strength DESC NULLS LAST, links DESC "
        "LIMIT {lim:UInt32}", {"d": registrable_domain(domain) or domain, "lim": limit})
    return [
        {
            **r,
            "source_domain": r["domain"],
            "links": int(r["links"]),
            "domain_strength": float(r["strength"]) if r.get("strength") is not None else None,
            "first_seen": str(r["first_seen"]),
            "last_seen": str(r["last_seen"]),
            "sources": list(r.get("sources") or []),
        }
        for r in rows
    ]


def intersect(ch: ClickHouse, you: str, competitors: list[str], min_competitors: int = 2, limit: int = 200) -> dict:
    """Link prospects: domains linking to ≥ min_competitors competitors but not to you."""
    you = registrable_domain(you) or you
    comps = [registrable_domain(c) or c for c in competitors][:5]
    rows = ch.query(
        "SELECT source_domain AS domain, groupUniqArray(target_domain) AS links_to, uniqExact(target_domain) AS n, any(s.score) AS strength "
        "FROM backlinks AS b FINAL LEFT JOIN (SELECT registrable_domain, argMax(score, version) AS score FROM domain_strength "
        "GROUP BY registrable_domain) AS s ON s.registrable_domain = b.source_domain "
        "WHERE status = 'live' AND target_domain IN {c:Array(String)} "
        "AND source_domain NOT IN (SELECT source_domain FROM backlinks FINAL WHERE target_domain = {you:String} AND status = 'live') "
        "GROUP BY domain HAVING n >= {m:UInt8} ORDER BY n DESC, strength DESC NULLS LAST, domain LIMIT {lim:UInt32}",
        {"c": comps, "you": you, "m": min_competitors, "lim": limit})
    prospects = [
        {
            **r,
            "source_domain": r["domain"],
            "competitors_linked": list(r.get("links_to") or []),
            "domain_strength": float(r["strength"]) if r.get("strength") is not None else None,
        }
        for r in rows
    ]
    return {"you": you, "competitors": comps, "prospects": prospects,
            "note": "Linking to competitors does not mean a site will link to you; review relevance before outreach."}


def risky_links(ch: ClickHouse, domain: str, keywords: list[str] | None = None) -> dict:
    """7-criteria evidence-backed Backlink Audit with risk_score, confidence_score, and recommended_action."""
    d = registrable_domain(domain) or domain
    p = {"d": d}
    now_iso = datetime.now(UTC).isoformat()
    signals = []
    per_domain_signals: dict[str, list[dict]] = defaultdict(list)

    # Criterion 4: Link network / PBN / link farm pattern (links to >= 50 distinct target domains)
    farm = ch.query("SELECT b.source_domain AS sd, uniqExact(o.target_domain) AS targets FROM backlinks AS b FINAL "
                    "INNER JOIN backlinks AS o ON o.source_domain = b.source_domain WHERE b.target_domain = {d:String} "
                    "GROUP BY sd HAVING targets >= 50 ORDER BY targets DESC LIMIT 100", p)
    for r in farm:
        sig = {
            "domain": r["sd"],
            "signal": "link_farm_pattern",
            "weight": 45,
            "evidence": {"distinct_target_domains_in_index": int(r["targets"])},
            "explanation": "The referring domain links to an unusually large number of unrelated domains in our index.",
        }
        signals.append(sig)
        per_domain_signals[r["sd"]].append(sig)

    # Criterion 5: Sitewide footer / sidebar links
    sitewide = ch.query("SELECT source_domain AS sd, count() AS links, uniqExact(source_url) AS pages FROM backlinks FINAL "
                        "WHERE target_domain = {d:String} AND link_position IN ('footer', 'sidebar', 'nav') GROUP BY sd HAVING pages >= 20", p)
    for r in sitewide:
        sig = {
            "domain": r["sd"],
            "signal": "sitewide_footer_links",
            "weight": 30,
            "evidence": {"pages": int(r["pages"]), "links": int(r["links"])},
            "explanation": "Footer/sidebar link repeated across many pages of one site (often paid/partner/widget links).",
        }
        signals.append(sig)
        per_domain_signals[r["sd"]].append(sig)

    # Criterion 3: Unnatural / over-optimized anchor text
    anchors = ch.query("SELECT anchor, count() AS n FROM backlinks FINAL WHERE target_domain = {d:String} AND status = 'live' GROUP BY anchor", p)
    total = sum(int(a["n"]) for a in anchors)
    if total >= 20:
        exact = sum(int(a["n"]) for a in anchors if classify_anchor(a["anchor"], d, keywords) == "exact")
        if exact / total > 0.3:
            signals.append({"domain": d, "signal": "excessive_exact_match_anchors", "evidence": {"exact_share": round(exact / total, 2)},
                            "explanation": "A high share of exact-match commercial anchors can look manipulative."})

    # TLD cluster check
    tld = ch.query("SELECT arrayElement(splitByChar('.', source_domain), -1) AS tld, uniqExact(source_domain) AS rd FROM backlinks FINAL "
                   "WHERE target_domain = {d:String} GROUP BY tld", p)
    rd_total = sum(int(t["rd"]) for t in tld) or 1
    for t in tld:
        if int(t["rd"]) >= 10 and int(t["rd"]) / rd_total > 0.4 and t["tld"] not in (d.split(".")[-1], "com", "org", "net"):
            signals.append({"domain": f"*.{t['tld']}", "signal": "unusual_tld_cluster", "evidence": {"share": round(int(t["rd"]) / rd_total, 2)},
                            "explanation": "A large share of referring domains comes from one unrelated TLD."})

    # Per-referring-domain audit across all 7 criteria
    dom_rows = ch.query(
        "SELECT b.source_domain AS sd, count() AS total_links, countIf(b.status = 'live') AS live_links, "
        "countIf(b.status = 'lost') AS lost_links, groupUniqArray(10)(b.anchor) AS sample_anchors, "
        "groupUniqArray(5)(b.source_url) AS sample_urls, max(b.first_seen) AS max_first_seen, min(b.first_seen) AS min_first_seen, "
        "any(s.score) AS ds_score, any(s.spam_penalty) AS spam_penalty "
        "FROM backlinks AS b FINAL LEFT JOIN (SELECT registrable_domain, argMax(score, version) AS score, "
        "argMax(spam_penalty, version) AS spam_penalty FROM domain_strength GROUP BY registrable_domain) AS s "
        "ON s.registrable_domain = b.source_domain WHERE b.target_domain = {d:String} GROUP BY sd ORDER BY total_links DESC LIMIT 500",
        p,
    )

    audited_domains: list[dict] = []
    for r in dom_rows:
        sd = r["sd"]
        sd_tld = sd.split(".")[-1].lower() if "." in sd else ""
        dom_sigs = list(per_domain_signals.get(sd, []))
        sample_anchors = [str(a) for a in (r.get("sample_anchors") or [])]
        sample_urls = [str(u) for u in (r.get("sample_urls") or [])]
        combined_text = f"{sd} {' '.join(sample_anchors)} {' '.join(sample_urls)}".lower()

        # Criterion 1: Low quality or spammy domain
        ds_val = float(r["ds_score"]) if r.get("ds_score") is not None else None
        spam_pen = float(r["spam_penalty"]) if r.get("spam_penalty") is not None else 0.0
        if sd_tld in SPAMMY_TLDS or spam_pen >= 0.25 or (ds_val is not None and ds_val < 8.0 and int(r["total_links"]) >= 5):
            dom_sigs.append({
                "domain": sd,
                "signal": "low_quality_or_spammy_domain",
                "weight": 25,
                "evidence": {"tld": sd_tld, "domain_strength": ds_val, "spam_penalty": spam_pen},
                "explanation": "Referring domain uses a high-spam TLD or exhibits low domain authority with spam graph penalty.",
            })

        # Criterion 2: Irrelevant niche / toxic topic tokens
        matched_spam_tokens = [tok.strip() for tok in SPAMMY_TOKENS if tok in combined_text]
        if matched_spam_tokens:
            dom_sigs.append({
                "domain": sd,
                "signal": "irrelevant_or_toxic_niche",
                "weight": 35,
                "evidence": {"matched_tokens": matched_spam_tokens},
                "explanation": "Referring URL or anchor text contains high-risk irrelevant niche terms (gambling/adult/link-directory).",
            })

        # Criterion 3 (per domain): Unnatural anchor text
        if keywords and any(classify_anchor(a, d, keywords) == "exact" for a in sample_anchors) and int(r["total_links"]) >= 3:
            dom_sigs.append({
                "domain": sd,
                "signal": "unnatural_anchor_text",
                "weight": 20,
                "evidence": {"sample_anchors": sample_anchors[:5]},
                "explanation": "Repeated exact-match commercial anchor text from the same referring domain.",
            })

        # Criterion 6: Sudden unnatural velocity spike (>= 20 links acquired on the exact same date)
        if int(r["total_links"]) >= 20 and str(r["min_first_seen"]) == str(r["max_first_seen"]):
            dom_sigs.append({
                "domain": sd,
                "signal": "unnatural_velocity_spike",
                "weight": 20,
                "evidence": {"links_on_single_day": int(r["total_links"]), "date": str(r["max_first_seen"])},
                "explanation": "Sudden burst of backlinks created on a single day from one domain.",
            })

        # Criterion 7: Unreachable / lost source pages
        if int(r["lost_links"]) > 0 and int(r["live_links"]) == 0:
            dom_sigs.append({
                "domain": sd,
                "signal": "unreachable_or_lost_source",
                "weight": 15,
                "evidence": {"lost_links": int(r["lost_links"])},
                "explanation": "All backlinks from this referring domain are currently unreachable or removed.",
            })

        risk_score = min(100, sum(int(s.get("weight", 25)) for s in dom_sigs))
        if risk_score >= 60:
            risk_level = "high"
            recommended_action = "disavow_candidate"
        elif risk_score >= 30:
            risk_level = "medium"
            recommended_action = "contact_for_removal" if risk_score >= 45 else "review"
        else:
            risk_level = "low"
            recommended_action = "keep"

        conf_score = round(min(0.95, 0.55 + 0.12 * len(dom_sigs)), 2) if dom_sigs else 0.8
        audited_domains.append({
            "source_domain": sd,
            "links_count": int(r["total_links"]),
            "live_links": int(r["live_links"]),
            "lost_links": int(r["lost_links"]),
            "domain_strength": ds_val,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "confidence_score": conf_score,
            "confidence_level": "HIGH" if conf_score >= 0.75 else "MEDIUM",
            "signals": [s["signal"] for s in dom_sigs],
            "evidence": {s["signal"]: s["evidence"] for s in dom_sigs},
            "explanations": [s["explanation"] for s in dom_sigs],
            "recommended_action": recommended_action,
            "sample_urls": sample_urls,
            "sample_anchors": sample_anchors,
        })

    audited_domains.sort(key=lambda x: (-x["risk_score"], -x["links_count"], x["source_domain"]))
    flagged_domains = [a for a in audited_domains if a["risk_score"] >= 25]
    warn_msg = "Disavow is rarely needed; Google ignores most spammy links. Use it only for links you believe caused a manual action."

    return {
        "domain": d,
        "signals": [{k: v for k, v in s.items() if k != "weight"} for s in signals],
        "audited_domains": audited_domains,
        "flagged_domains": flagged_domains,
        "summary": {
            "total_referring_domains_audited": len(audited_domains),
            "high_risk_count": sum(1 for a in audited_domains if a["risk_level"] == "high"),
            "medium_risk_count": sum(1 for a in audited_domains if a["risk_level"] == "medium"),
            "low_risk_count": sum(1 for a in audited_domains if a["risk_level"] == "low"),
            "disavow_candidates": sum(1 for a in audited_domains if a["recommended_action"] == "disavow_candidate"),
        },
        "coverage": coverage(ch, d),
        "warning": warn_msg,
        "disavow_warning": warn_msg,
        "provenance": {
            "dataSource": "Backlink_Risk_Auditor_v1",
            "data_source": "Backlink_Risk_Auditor_v1",
            "source_class": "OBSERVED",
            "collectedAt": now_iso,
            "collected_at": now_iso,
            "updatedAt": now_iso,
            "updated_at": now_iso,
            "isEstimated": False,
            "is_estimated": False,
            "confidenceScore": 0.82,
            "confidence_score": 0.82,
            "confidenceLevel": "HIGH",
            "confidence_level": "HIGH",
        },
    }


def disavow_file(domains: list[str], urls: list[str] | None = None) -> str:
    lines = ["# Generated by Growth Intelligence. Review carefully — disavowing good links can hurt.",
             "# Disavow is rarely necessary; Google ignores most low-quality links automatically."]
    if urls:
        for u in sorted({u.strip() for u in urls if u and u.strip().startswith(("http://", "https://"))}):
            lines.append(u)
    for d in sorted({registrable_domain(x) or x for x in domains if x and not x.startswith("*")}):
        lines.append(f"domain:{d}")
    return "\n".join(lines) + "\n"


def compute_domain_strength(ch: ClickHouse, max_edges: int = 5_000_000, damping: float = 0.85, iters: int = 30) -> dict:
    edges = ch.query(f"SELECT source_domain AS s, target_domain AS t, max(if(rel IN ('', 'follow'), 1.0, 0.1)) AS w "
                     f"FROM backlinks FINAL WHERE status = 'live' GROUP BY s, t LIMIT {int(max_edges)}")  # nosec B608 - int-cast limit only
    out = defaultdict(list)
    nodes, indeg = set(), Counter()
    for e in edges:
        out[e["s"]].append((e["t"], float(e["w"])))
        nodes |= {e["s"], e["t"]}
        indeg[e["t"]] += 1
    n = len(nodes)
    if n == 0:
        return {"domains": 0}
    pr = {u: 1 / n for u in nodes}
    for _ in range(iters):
        nxt = {u: (1 - damping) / n for u in nodes}
        dangling = 0.0
        for u in nodes:
            targets = out.get(u)
            if not targets:
                dangling += pr[u]
                continue
            tot = sum(w for _, w in targets)
            for t, w in targets:
                nxt[t] += damping * pr[u] * w / tot
        for u in nodes:
            nxt[u] += damping * dangling / n
        pr = nxt
    outdeg = {u: len(v) for u, v in out.items()}
    top = max(pr.values())
    rows, v = [], _v()
    for u in nodes:
        # spam penalty: domains whose inbound comes only from link-farm-like sources (very high out-degree)
        sources = [s for s, ts in out.items() if any(t == u for t, _ in ts)] if indeg[u] <= 20 else []
        farm_share = (sum(1 for s in sources if outdeg.get(s, 0) >= 50) / len(sources)) if sources else 0.0
        penalty = round(0.5 * farm_share, 3)
        score = 100 * math.log1p(pr[u] / top * 999) / math.log(1000) * (1 - penalty)
        rows.append({"registrable_domain": u, "score": round(score, 1), "raw_pagerank": pr[u], "referring_domains": indeg[u],
                     "spam_penalty": penalty, "model_version": DS_MODEL_VERSION,
                     "computed_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"), "version": v})
    ch.insert("domain_strength", rows)
    return {"domains": n, "edges": len(edges), "model_version": DS_MODEL_VERSION}


def strengths(ch: ClickHouse, domains: list[str]) -> dict[str, float]:
    if not domains:
        return {}
    rows = ch.query("SELECT registrable_domain AS d, argMax(score, version) AS s FROM domain_strength "
                    "WHERE registrable_domain IN {ds:Array(String)} GROUP BY d", {"ds": domains})
    return {r["d"]: float(r["s"]) for r in rows}


async def verify_links(ch: ClickHouse, target_domain: str, limit: int = 200, stale_days: int = 7, transport=None) -> dict:
    """Discovery re-check: refetch referring pages (robots-respecting, global per-host politeness) and mark links live/lost."""
    from urllib.robotparser import RobotFileParser

    from app.core.config import get_settings
    from app.infra.ratelimit import TokenBucket, host_key
    from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
    from app.services.crawler.parser import parse_html
    s = get_settings()
    d = registrable_domain(target_domain) or target_domain
    rows = ch.query("SELECT * FROM backlinks FINAL WHERE target_domain = {d:String} AND status != 'lost' "
                    "AND last_checked <= today() - {sd:UInt32} ORDER BY first_seen LIMIT {lim:UInt32}",
                    {"d": d, "sd": stale_days, "lim": limit})
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_source[r["source_url"]].append(r)
    bucket, robots = TokenBucket(), {}
    updated, live, lost, skipped = [], 0, 0, 0
    today = date.today().isoformat()
    for src, links in by_source.items():
        from app.services.crawler.urls import host_of
        host = host_of(src)
        policy = FetchPolicy(allowed_hosts=[host], user_agent=s.crawler_user_agent, timeout_s=15, max_bytes=3_000_000,
                             allow_private=s.crawler_allow_private_networks)
        async with build_client(policy, transport) as c:
            origin = "/".join(src.split("/")[:3])
            if origin not in robots:
                rr = await safe_get(c, origin + "/robots.txt", policy, accept_non_html=True)
                rp = RobotFileParser()
                rp.parse(rr.body.decode("utf-8", "replace").splitlines() if rr.status_code == 200 else [])
                robots[origin] = rp
            if not robots[origin].can_fetch(s.crawler_user_agent, src):
                skipped += len(links)
                continue
            await bucket.acquire(host_key(host), rate=1 / max(s.crawler_min_request_delay_ms / 1000, 0.001), burst=1)
            res = await safe_get(c, src, policy)
        found = set()
        if res.is_html and res.body:
            found = {l.href for l in parse_html(res.body, res.final_url or src, res.content_type).links}
        for r in links:
            alive = res.status_code == 200 and r["target_url"] in found
            live += alive
            lost += not alive
            updated.append({**r, "status": "live" if alive else "lost", "last_checked": today,
                            "last_seen": today if alive else r["last_seen"], "version": _v()})
    if updated:
        for r in updated:
            r["fetched_at"] = str(r["fetched_at"])
            r["first_seen"], r["last_seen"], r["last_checked"] = str(r["first_seen"]), str(r["last_seen"]), str(r["last_checked"])
        ch.insert("backlinks", updated)
    return {"checked_sources": len(by_source), "live": live, "lost": lost, "skipped_robots": skipped}


async def fetch_provider_backlinks(db, ch: ClickHouse, org_id: int, domain: str, limit: int, confirmed: bool, request_id: str) -> dict:
    from app.providers import registry, runtime
    from app.services.keywords import service as kw_service
    d = registrable_domain(domain) or domain
    provider = registry.get("backlinks", db, org_id)
    kw_service._charge_or_preflight(db, org_id, "backlink_query", max(1, limit // 100), confirmed, f"bl:{request_id}")
    res = await runtime.call(db, provider, "backlinks", {"d": d, "limit": limit}, lambda: provider.backlinks(d, limit),
                             org_id=org_id, archive=kw_service._archive())
    synthetic = any((r.get("lineage") or {}).get("is_synthetic") for r in res["data"])
    n = ingest(ch, res["data"], f"provider:{provider.name}", "DEMO" if synthetic else "PROVIDER", is_synthetic=synthetic)
    return {"domain": d, "ingested": n, "cached": res["cached"], "provider": provider.name}


def reclamation_opportunities(db, ch: ClickHouse, site) -> dict:
    """Backlinks pointing at URLs that return 404/410 in the latest own crawl → 301 redirect opportunities."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models import CrawlPage, CrawlRun, GrowthOpportunity
    d = _dom(site.base_url)
    crawl = db.scalar(select(CrawlRun).where(CrawlRun.site_id == site.id, CrawlRun.competitor_id.is_(None),
                                             CrawlRun.status == "completed").order_by(CrawlRun.id.desc()).limit(1))
    if not crawl:
        return {"available": False, "reason": "needs a completed crawl"}
    broken = {u: c for u, c in db.execute(select(CrawlPage.normalized_url, CrawlPage.status_code)
                                          .where(CrawlPage.crawl_run_id == crawl.id, CrawlPage.status_code.in_((404, 410))))}
    links = ch.query("SELECT target_url, uniqExact(source_domain) AS rd, groupUniqArray(10)(source_url) AS sources, "
                     "groupUniqArray(source) AS src FROM backlinks FINAL WHERE target_domain = {d:String} AND status = 'live' "
                     "GROUP BY target_url", {"d": d})
    created = 0
    now = datetime.now(UTC)
    for r in links:
        from app.services.crawler.urls import normalize_url
        tu = normalize_url(r["target_url"])
        if tu not in broken:
            continue
        key = f"broken_backlink_reclamation|{tu}"
        o = db.scalar(select(GrowthOpportunity).where(GrowthOpportunity.site_id == site.id, GrowthOpportunity.key == key))
        if o is None:
            o = GrowthOpportunity(site_id=site.id, key=key, status="new")
            db.add(o)
            created += 1
        rd = int(r["rd"])
        o.url, o.opportunity_type = tu, "broken_backlink_reclamation"
        o.title = f"Reclaim {rd} referring domain(s) pointing to a {broken[tu]} page"
        o.description = "External sites link to a URL that now returns an error; their link equity is lost."
        o.evidence = {"status_code": broken[tu], "referring_domains": rd, "sample_sources": r["sources"], "link_sources": r["src"],
                      "crawl_id": crawl.id}
        o.data_sources = [f"crawl:own#{crawl.id}"] + [f"backlinks:{s}" for s in r["src"]]
        o.date_range = None
        o.impact_estimate = {"metric": "referring_domains_recovered", "value": rd, "unit": "domains", "method": "count of live links in our index",
                             "source_class": "OBSERVED"}
        o.confidence, o.effort = 0.7, "low"
        o.priority_score = round(min(100.0, 20 + rd * 5) * 0.7, 2)
        o.recommended_action = "301-redirect the URL to the most relevant live page (or restore it)."
        o.validation_metric = "Target URL returns a single-hop 301 to a 200 page; links remain live on next verification"
        o.limitations = "Our backlink index is partial; more links may point to this URL."
        o.last_seen_at = now
    db.commit()
    from app.services.crawler.urls import normalize_url as _n
    return {"available": True, "created": created, "broken_urls_with_links": sum(1 for r in links if _n(r["target_url"]) in broken)}


from app import jobs as _jobs  # noqa: E402  (job handlers)


@_jobs.handler("backlink_verify")
async def _verify_job(ctx, payload):
    return await verify_links(ClickHouse(), payload["domain"], payload.get("limit", 200))


@_jobs.handler("domain_strength")
async def _strength_job(ctx, payload):
    import asyncio
    return await asyncio.to_thread(compute_domain_strength, ClickHouse())


@_jobs.handler("cc_import")
async def _cc_job(ctx, payload):
    import asyncio

    from app.services import commoncrawl
    start = int(ctx.checkpoint.get("offset", 0))
    loop = asyncio.get_running_loop()

    def cp(state):
        asyncio.run_coroutine_threadsafe(ctx.heartbeat(checkpoint=state, progress=state), loop).result()
    return await asyncio.to_thread(commoncrawl.import_stream, ClickHouse(), commoncrawl.http_chunks(payload["path"], start),
                                   {t.lower() for t in payload["targets"]}, start_offset=start,
                                   max_records=payload.get("max_records", 200_000), checkpoint=cp)
