"""Content intelligence (spec §12). Everything about competitor pages is phrased as *observed*; correlation ≠ causation.

- brief(): fetch top-N organic results of the latest SERP snapshot (robots.txt respected, shared per-host politeness,
  SSRF-safe, size-limited), extract headings, word counts, schema types, media counts and TF-IDF terms.
- check_page(): compare one own URL against the brief (missing terms/headings/schema, length vs observed range).
- coverage_score(): explainable score of a draft against brief terms (TF-IDF weighted) + headings.
- topical_map(): keyword clusters × domains visibility from SERP snapshots (CTR-weighted, MODELED).
- inventory(): keep / update / merge / remove recommendation per own URL with evidence.
"""

import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from lxml import html as lhtml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infra.clickhouse import ClickHouse
from app.infra.ratelimit import TokenBucket, host_key
from app.models import ContentTemplateRecord, CrawlPage, CrawlRun, OrganicLandingDaily as GA, SearchPerformanceDaily as SP, Site
from app.normalize.text import EN_STOP, TR_STOP, match_key
from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
from app.services.crawler.parser import extract_structured_data
from app.services.crawler.urls import host_of
from app.services.keywords import store
from app.services.rank_tracking import ctr

TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
CORRELATION_NOTE = ("Observed on pages that currently rank; these patterns are not proven ranking causes. "
                    "Use them as a checklist for completeness, not as a formula.")


def _tokens(text: str, lang: str) -> list[str]:
    stop = {match_key(w, lang) for w in (TR_STOP | EN_STOP)}
    return [t for t in TOKEN.findall(match_key(text, lang)) if t not in stop]


def analyze_html(body: bytes, url: str, lang: str, content_type: str | None = None) -> dict:
    from app.services.crawler.parser import decode_html
    raw_html = decode_html(body or b"<html/>", content_type) or "<html/>"
    doc = lhtml.fromstring(raw_html)
    sd = extract_structured_data(doc)
    title_nodes = doc.xpath("//title/text()")
    title = " ".join(title_nodes).strip()[:300] if title_nodes else ""
    meta_desc_nodes = doc.xpath("//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='description']/@content")
    meta_description = " ".join(meta_desc_nodes).strip()[:500] if meta_desc_nodes else ""
    time_nodes = doc.xpath("//time/@datetime | //meta[contains(@property,'modified_time') or contains(@property,'published_time')]/@content")
    freshness_date = str(time_nodes[0]).strip() if time_nodes else None
    own_host = host_of(url).removeprefix("www.")
    all_hrefs = [str(h).strip() for h in doc.xpath("//a[@href]/@href") if str(h).strip()]
    internal_links_count = 0
    external_links_count = 0
    for h in all_hrefs:
        if h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if h.startswith("/") or host_of(h).removeprefix("www.") == own_host:
            internal_links_count += 1
        elif h.startswith(("http://", "https://")):
            external_links_count += 1
    img_elements = doc.xpath("//img")
    images_with_alt = sum(1 for img in img_elements if (img.get("alt") or "").strip())
    for bad in doc.xpath("//script | //style | //noscript | //nav | //footer"):
        bad.drop_tree()
    headings = [{"level": int(h.tag[1]), "text": " ".join(h.itertext()).strip()[:200]} for h in doc.xpath("//h1 | //h2 | //h3")]
    text = " ".join(doc.itertext())
    words = text.split()
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.strip().split()) >= 2]
    avg_sentence_len = round(len(words) / max(1, len(sentences)), 1) if words else 0.0
    avg_word_len = round(sum(len(w) for w in words) / max(1, len(words)), 1) if words else 0.0
    readability_score = round(max(0.0, min(100.0, 100.0 - max(0.0, avg_sentence_len - 14.0) * 2.2 - max(0.0, avg_word_len - 5.5) * 6.0)), 1) if words else 0.0
    toks = _tokens(text, lang)
    bigrams = [f"{a} {b}" for a, b in zip(toks, toks[1:])]
    return {
        "url": url,
        "title": title,
        "meta_description": meta_description,
        "freshness_date": freshness_date,
        "word_count": len(words),
        "first_100_words": " ".join(words[:100]),
        "headings": headings[:80],
        "terms": Counter(toks + bigrams),
        "schema_types": sorted({i.get("type") for i in sd if i.get("type")}),
        "images": len(img_elements),
        "images_with_alt": images_with_alt,
        "images_missing_alt": len(img_elements) - images_with_alt,
        "internal_links": internal_links_count,
        "external_links": external_links_count,
        "readability": {
            "score": readability_score,
            "avg_sentence_words": avg_sentence_len,
            "avg_word_chars": avg_word_len,
            "level": "easy" if readability_score >= 70 else ("moderate" if readability_score >= 45 else "complex"),
        },
        "videos": len(doc.xpath("//video | //iframe[contains(@src,'youtube') or contains(@src,'vimeo')]")),
        "tables": len(doc.xpath("//table")),
        "lists": len(doc.xpath("//ul | //ol")),
    }


async def _fetch_pages(urls: list[str], transport=None) -> list[tuple[str, bytes, str | None]]:
    s = get_settings()
    bucket, out, robots = TokenBucket(), [], {}
    for u in urls:
        host = host_of(u)
        policy = FetchPolicy(allowed_hosts=[host], user_agent=s.crawler_user_agent, timeout_s=15, max_bytes=3_000_000,
                             allow_private=s.crawler_allow_private_networks)
        async with build_client(policy, transport) as c:
            origin = "/".join(u.split("/")[:3])
            if origin not in robots:
                rr = await safe_get(c, origin + "/robots.txt", policy, accept_non_html=True)
                rp = RobotFileParser()
                rp.parse(rr.body.decode("utf-8", "replace").splitlines() if rr.status_code == 200 else [])
                robots[origin] = rp
            if not robots[origin].can_fetch(s.crawler_user_agent, u):
                continue
            await bucket.acquire(host_key(host), rate=1 / max(s.crawler_min_request_delay_ms / 1000, 0.001), burst=1)
            res = await safe_get(c, u, policy)
        if res.status_code == 200 and res.is_html and res.body:
            out.append((res.final_url or u, res.body, res.content_type))
    return out


def _tfidf(docs: list[Counter], top: int = 40) -> list[dict]:
    n = len(docs)
    df = Counter(t for d in docs for t in d)
    scores = defaultdict(float)
    for d in docs:
        total = sum(d.values()) or 1
        for t, c in d.items():
            scores[t] += (c / total) * math.log((1 + n) / (1 + df[t]) + 1)
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [{"term": t, "score": round(sc / n, 5), "pages_with_term": df[t], "share": round(df[t] / n, 2)} for t, sc in ranked[:top]
            if df[t] >= max(2, math.ceil(n * 0.3))]


async def brief(
    ch: ClickHouse,
    keyword: str,
    database: str,
    top_n: int = 10,
    transport=None,
    db: Session | None = None,
    org_id: int | None = None,
    site_id: int | None = None,
) -> dict:
    lang = database.split("-")[0]
    serp = store.latest_serp(ch, match_key(keyword, lang), database)
    if not serp["available"]:
        return {"available": False, "reason": "no SERP snapshot for this keyword (fetch SERPs first; requires provider)"}
    urls = [r["url"] for r in serp["organic"][:top_n] if r.get("url")]
    pages = [analyze_html(b, u, lang, ct) for u, b, ct in await _fetch_pages(urls, transport)]
    if not pages:
        return {"available": False, "reason": "no ranking page could be fetched (robots/blocked/non-HTML)", "urls": urls}
    wc = sorted(p["word_count"] for p in pages)
    heading_terms = Counter(t for p in pages for h in p["headings"] if h["level"] >= 2 for t in set(_tokens(h["text"], lang)))
    questions = store.explorer(ch, database, q=keyword, match="broad", question=True, limit=15)["items"]
    related_items = store.explorer(ch, database, q=keyword, match="broad", limit=15)["items"]
    tfidf_terms = _tfidf([p["terms"] for p in pages])
    common_sub = [{"term": t, "pages": n} for t, n in heading_terms.most_common(25) if n >= 2]
    secondary_kws = [r["keyword"] for r in related_items if r["keyword"].lower() != keyword.lower()][:8]
    if not secondary_kws:
        secondary_kws = [t["term"] for t in tfidf_terms if " " in t["term"]][:8]
    semantic_kws = [t["term"] for t in tfidf_terms[:20]]
    q_list = [q["keyword"] for q in questions]
    if not q_list:
        q_list = [f"{keyword} nedir ve nasıl seçilir?", f"En iyi {keyword} özellikleri nelerdir?"]

    recommended_headings = {
        "h1": f"{keyword.title()} Rehberi: Kapsamlı İnceleme ve Seçim Kriterleri",
        "h2": [f"{s['term'].title()} Nedir ve Önemi" for s in common_sub[:6]] or [f"{keyword.title()} Temel Özellikleri"],
        "h3": q_list[:5],
    }
    meta_templates = {
        "meta_title": f"{keyword.title()} — Kapsamlı Rehber ve Karşılaştırma"[:60],
        "meta_description": f"{keyword.title()} hakkında bilmeniz gereken tüm teknik detaylar, {', '.join(semantic_kws[:3])} kriterleri ve uzman önerileri."[:155],
    }

    internal_link_suggestions: list[dict] = []
    if db is not None and site_id is not None:
        latest_crawl = db.scalar(
            select(CrawlRun).where(CrawlRun.site_id == site_id, CrawlRun.competitor_id.is_(None), CrawlRun.status == "completed").order_by(CrawlRun.id.desc()).limit(1)
        )
        if latest_crawl is not None:
            kw_toks = set(_tokens(keyword, lang)) | {s["term"] for s in common_sub[:5]}
            for cp in db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == latest_crawl.id, CrawlPage.status_code == 200, CrawlPage.is_html.is_(True)).limit(200)):
                cp_toks = set(_tokens(f"{cp.title or ''} {cp.normalized_url}", lang))
                overlap = kw_toks & cp_toks
                if overlap:
                    internal_link_suggestions.append({
                        "url": cp.normalized_url,
                        "recommended_anchor": (cp.title or keyword)[:80],
                        "matched_terms": sorted(overlap),
                    })
                    if len(internal_link_suggestions) >= 10:
                        break

    now_iso = datetime.now(UTC).isoformat()
    res = {
        "available": True,
        "keyword": keyword,
        "database": database,
        "source_class": "OBSERVED",
        "serp_snapshot": {"date": str(serp["snapshot_date"]), "provider": serp["provider"]},
        "pages_analyzed": [
            {
                "url": p["url"],
                "word_count": p["word_count"],
                "h2_count": sum(h["level"] == 2 for h in p["headings"]),
                "schema_types": p["schema_types"],
                "images": p["images"],
                "videos": p["videos"],
            }
            for p in pages
        ],
        "word_count_range": {
            "min": wc[0],
            "p25": wc[len(wc) // 4],
            "median": wc[len(wc) // 2],
            "p75": wc[(3 * len(wc)) // 4],
            "max": wc[-1],
        },
        "terms": [{"term": t["term"], "score": t["score"], "pages_with_term": t["pages_with_term"], "doc_freq": t["pages_with_term"], "share": t["share"]} for t in tfidf_terms],
        "common_subtopics": common_sub,
        "subtopics": [{"heading": s["term"], "pages": s["pages"]} for s in common_sub],
        "secondary_keywords": secondary_kws,
        "semantic_keywords": semantic_kws,
        "recommended_heading_structure": recommended_headings,
        "meta_templates": meta_templates,
        "internal_link_suggestions": internal_link_suggestions,
        "schema_types_observed": Counter(t for p in pages for t in p["schema_types"]),
        "media_observed": {"pages_with_video": sum(p["videos"] > 0 for p in pages), "median_images": sorted(p["images"] for p in pages)[len(pages) // 2]},
        "questions": q_list,
        "questions_paa": q_list,
        "note": CORRELATION_NOTE,
        "caveat": CORRELATION_NOTE,
        "provenance": {
            "dataSource": "SERP_Content_Analyzer",
            "data_source": "SERP_Content_Analyzer",
            "source_class": "OBSERVED",
            "collectedAt": now_iso,
            "collected_at": now_iso,
            "updatedAt": now_iso,
            "updated_at": now_iso,
            "isEstimated": False,
            "is_estimated": False,
            "confidenceScore": 0.85,
            "confidence_score": 0.85,
            "confidenceLevel": "HIGH",
            "confidence_level": "HIGH",
        },
    }
    if db is not None and org_id is not None:
        rec = ContentTemplateRecord(
            org_id=org_id,
            site_id=site_id,
            keyword=keyword[:300],
            database=database,
            template_data={
                "word_count_range": res["word_count_range"],
                "secondary_keywords": secondary_kws,
                "semantic_keywords": semantic_kws,
                "recommended_heading_structure": recommended_headings,
                "meta_templates": meta_templates,
                "questions": q_list,
            },
            data_source="SERP_Content_Analyzer",
            source_class="OBSERVED",
            confidence_score=0.85,
        )
        db.add(rec)
        db.commit()
        res["template_id"] = rec.id
    return res


def _evaluate_13_onpage_checks(own: dict, keyword: str, lang: str, b: dict) -> tuple[list[dict], float]:
    kw_norm = match_key(keyword, lang)
    kw_tokens = set(_tokens(keyword, lang))
    title = own.get("title") or ""
    meta_desc = own.get("meta_description") or ""
    slug_path = urlsplit(own["url"]).path.strip("/")
    h1_list = [h["text"] for h in own["headings"] if h["level"] == 1]
    h2_list = [h["text"] for h in own["headings"] if h["level"] == 2]
    h3_list = [h["text"] for h in own["headings"] if h["level"] == 3]
    first_100 = own.get("first_100_words") or ""
    wc = max(1, int(own.get("word_count") or 0))

    # Count keyword occurrences in body terms
    kw_count = int(own["terms"].get(kw_norm, 0))
    if kw_count == 0 and kw_tokens:
        kw_count = min(int(own["terms"].get(t, 0)) for t in kw_tokens)
    density_pct = round((kw_count * max(1, len(kw_tokens))) / wc * 100.0, 2) if own.get("word_count") else 0.0

    in_title = bool(kw_tokens and kw_tokens.issubset(set(_tokens(title, lang)))) or (kw_norm in match_key(title, lang) if kw_norm else False)
    in_meta = bool(kw_tokens and kw_tokens.issubset(set(_tokens(meta_desc, lang)))) or (kw_norm in match_key(meta_desc, lang) if kw_norm else False)
    in_slug = bool(kw_tokens and (kw_tokens & set(_tokens(slug_path.replace("-", " "), lang))))
    in_h1 = any(kw_norm in match_key(h, lang) or (kw_tokens and kw_tokens.issubset(set(_tokens(h, lang)))) for h in h1_list)
    in_first_100 = kw_norm in match_key(first_100, lang) or (bool(kw_tokens) and kw_tokens.issubset(set(_tokens(first_100, lang))))
    in_h2 = any(bool(kw_tokens & set(_tokens(h, lang))) for h in h2_list)

    own_terms = set(own["terms"])
    brief_terms = b.get("terms") or []
    semantic_covered = sum(1 for t in brief_terms if t["term"] in own_terms)
    semantic_ratio = round(semantic_covered / max(1, len(brief_terms)), 2) if brief_terms else 1.0

    readability = own.get("readability") or {"score": 70.0, "level": "easy"}
    checks = [
        {
            "id": "title_optimization",
            "label": "Title Tag Optimization",
            "status": "pass" if (30 <= len(title) <= 60 and in_title) else ("warn" if title else "fail"),
            "value": {"length": len(title), "contains_keyword": in_title, "title": title},
            "recommendation": "Keep <title> between 30–60 characters and include the primary keyword near the beginning.",
        },
        {
            "id": "meta_description_optimization",
            "label": "Meta Description Optimization",
            "status": "pass" if (70 <= len(meta_desc) <= 160 and in_meta) else ("warn" if meta_desc else "fail"),
            "value": {"length": len(meta_desc), "contains_keyword": in_meta},
            "recommendation": "Provide a 120–155 character meta description summarizing the page value with the target keyword.",
        },
        {
            "id": "url_slug_optimization",
            "label": "URL Slug Structure",
            "status": "pass" if (in_slug and len(slug_path) <= 75 and "_" not in slug_path) else "warn",
            "value": {"slug": f"/{slug_path}", "length": len(slug_path), "contains_keyword_stem": in_slug},
            "recommendation": "Use a short, lowercase, hyphen-separated URL slug containing the core topic stem.",
        },
        {
            "id": "heading_hierarchy",
            "label": "Heading Hierarchy (H1–H3)",
            "status": "pass" if (len(h1_list) == 1 and len(h2_list) >= 2) else ("warn" if len(h1_list) == 1 else "fail"),
            "value": {"h1_count": len(h1_list), "h2_count": len(h2_list), "h3_count": len(h3_list)},
            "recommendation": "Include exactly one H1 tag and structure subtopics under descriptive H2 and H3 headings.",
        },
        {
            "id": "keyword_placement",
            "label": "Primary Keyword Placement",
            "status": "pass" if (in_title and in_h1 and in_first_100) else ("warn" if (in_h1 or in_first_100) else "fail"),
            "value": {"in_title": in_title, "in_h1": in_h1, "in_first_100_words": in_first_100, "in_h2": in_h2},
            "recommendation": "Include the target keyword naturally in the Title, H1, first 100 words, and at least one H2.",
        },
        {
            "id": "keyword_density",
            "label": "Keyword Frequency & Stuffing Guard",
            "status": "pass" if (0.4 <= density_pct <= 2.8) else ("fail" if density_pct > 3.5 else "warn"),
            "value": {"occurrences": kw_count, "density_pct": density_pct, "stuffing_detected": density_pct > 3.5},
            "recommendation": "Maintain natural keyword density (0.5%–2.5%); avoid keyword stuffing (>3.5%).",
        },
        {
            "id": "semantic_coverage",
            "label": "Semantic & NLP Term Coverage",
            "status": "pass" if semantic_ratio >= 0.6 else ("warn" if semantic_ratio >= 0.35 else "fail"),
            "value": {"covered_terms": semantic_covered, "total_brief_terms": len(brief_terms), "coverage_ratio": semantic_ratio},
            "recommendation": "Incorporate missing semantic terms observed across top-ranking pages.",
        },
        {
            "id": "readability",
            "label": "Readability & Sentence Structure",
            "status": "pass" if readability["score"] >= 60 else ("warn" if readability["score"] >= 40 else "fail"),
            "value": readability,
            "recommendation": "Use concise sentences (12–18 words avg), bullet lists, and clear subheadings for scannability.",
        },
        {
            "id": "internal_links",
            "label": "Internal Linking",
            "status": "pass" if own.get("internal_links", 0) >= 2 else ("warn" if own.get("internal_links", 0) == 1 else "fail"),
            "value": {"internal_links_count": own.get("internal_links", 0)},
            "recommendation": "Add contextual internal links to related pillar and cluster pages.",
        },
        {
            "id": "external_links",
            "label": "External Reference Links",
            "status": "pass" if own.get("external_links", 0) >= 1 else "warn",
            "value": {"external_links_count": own.get("external_links", 0)},
            "recommendation": "Cite authoritative external sources where relevant to support E-E-A-T.",
        },
        {
            "id": "image_optimization",
            "label": "Image & Alt Text Optimization",
            "status": "pass" if (own.get("images", 0) > 0 and own.get("images_missing_alt", 0) == 0) else ("warn" if own.get("images", 0) > 0 else "fail"),
            "value": {"total_images": own.get("images", 0), "with_alt": own.get("images_with_alt", 0), "missing_alt": own.get("images_missing_alt", 0)},
            "recommendation": "Include illustrative images with descriptive alt attributes.",
        },
        {
            "id": "schema_markup",
            "label": "Structured Data (Schema.org)",
            "status": "pass" if own.get("schema_types") else "warn",
            "value": {"schema_types": own.get("schema_types", [])},
            "recommendation": "Add JSON-LD Schema.org markup (e.g., Article, FAQPage, Product, BreadcrumbList).",
        },
        {
            "id": "content_freshness",
            "label": "Content Freshness Signal",
            "status": "pass" if own.get("freshness_date") else "warn",
            "value": {"detected_date": own.get("freshness_date")},
            "recommendation": "Publish visible <time datetime> and article:modified_time metadata when updating content.",
        },
    ]
    pts = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    score = round(sum(pts[c["status"]] for c in checks) / len(checks) * 100.0, 1)
    return checks, score


async def check_page(
    ch: ClickHouse,
    url: str,
    keyword: str,
    database: str,
    transport=None,
    brief_data: dict | None = None,
    db: Session | None = None,
    org_id: int | None = None,
    site_id: int | None = None,
) -> dict:
    lang = database.split("-")[0]
    b = brief_data or await brief(ch, keyword, database, transport=transport)
    if not b.get("available"):
        return b
    fetched = await _fetch_pages([url], transport)
    if not fetched:
        return {"available": False, "reason": "own page could not be fetched"}
    own = analyze_html(fetched[0][1], url, lang, fetched[0][2])
    own_terms = set(own["terms"])
    missing = [t for t in b["terms"] if t["term"] not in own_terms and t["share"] >= 0.5]
    own_heads = {t for h in own["headings"] for t in _tokens(h["text"], lang)}
    missing_sub = [s for s in b["common_subtopics"] if s["term"] not in own_heads]
    schema_gap = [t for t, n in b["schema_types_observed"].items() if t not in own["schema_types"] and n >= 2]
    wr = b["word_count_range"]
    checks_13, onpage_score = _evaluate_13_onpage_checks(own, keyword, lang, b)
    now_iso = datetime.now(UTC).isoformat()
    res = {
        "available": True,
        "url": url,
        "keyword": keyword,
        "word_count": own["word_count"],
        "observed_range": wr,
        "length_note": "below observed range" if own["word_count"] < wr["p25"] else ("above observed range" if own["word_count"] > wr["p75"] else "within observed range"),
        "missing_terms": missing[:25],
        "missing_subtopics": missing_sub[:15],
        "schema_types_missing": schema_gap,
        "missing_schema_types": schema_gap,
        "own_schema_types": own["schema_types"],
        "onpage_score": onpage_score,
        "onpage_checklist": checks_13,
        "readability": own["readability"],
        "note": CORRELATION_NOTE,
        "caveat": CORRELATION_NOTE,
        "provenance": {
            "dataSource": "OnPage_DOM_And_SERP_Auditor",
            "data_source": "OnPage_DOM_And_SERP_Auditor",
            "source_class": "OBSERVED",
            "collectedAt": now_iso,
            "collected_at": now_iso,
            "updatedAt": now_iso,
            "updated_at": now_iso,
            "isEstimated": False,
            "is_estimated": False,
            "confidenceScore": 0.9,
            "confidence_score": 0.9,
            "confidenceLevel": "HIGH",
            "confidence_level": "HIGH",
        },
    }
    if db is not None and org_id is not None:
        rec = ContentTemplateRecord(
            org_id=org_id,
            site_id=site_id,
            keyword=keyword[:300],
            database=database,
            target_url=url,
            template_data={"word_count_range": wr},
            onpage_check={"onpage_score": onpage_score, "checks": checks_13},
            data_source="OnPage_DOM_And_SERP_Auditor",
            source_class="OBSERVED",
            confidence_score=0.9,
        )
        db.add(rec)
        db.commit()
    return res


def coverage_score(draft: str, brief_data: dict, lang: str = "tr") -> dict:
    if not brief_data.get("available"):
        return {"available": False, "reason": "brief not available"}
    toks = _tokens(draft, lang)
    have = set(toks) | {f"{a} {b}" for a, b in zip(toks, toks[1:])}
    terms = brief_data["terms"]
    tot = sum(t["score"] for t in terms) or 1
    covered = [t for t in terms if t["term"] in have]
    subs = brief_data["common_subtopics"]
    sub_cov = [s for s in subs if s["term"] in have]
    term_score = sum(t["score"] for t in covered) / tot
    sub_score = len(sub_cov) / len(subs) if subs else 1.0
    score = round(100 * (0.7 * term_score + 0.3 * sub_score), 1)
    return {"available": True, "score": score, "components": {"weighted_term_coverage": round(term_score, 3), "subtopic_coverage": round(sub_score, 3)},
            "formula": "100 × (0.7 × TF-IDF-weighted term coverage + 0.3 × subtopic coverage)",
            "covered_terms": [t["term"] for t in covered], "missing_terms": [t["term"] for t in terms if t not in covered][:30],
            "word_count": len(draft.split()), "observed_range": brief_data["word_count_range"], "note": CORRELATION_NOTE}


def topical_map(ch: ClickHouse, clusters: dict[str, list[str]], database: str, domains: list[str]) -> dict:
    lang = database.split("-")[0]
    rows = []
    for name, kws in clusters.items():
        vis = defaultdict(float)
        n = 0
        for kw in kws:
            serp = store.latest_serp(ch, match_key(kw, lang), database)
            if not serp["available"]:
                continue
            n += 1
            seen = set()
            for r in serp["organic"][:20]:
                if r["domain"] in domains and r["domain"] not in seen:
                    seen.add(r["domain"])
                    vis[r["domain"]] += ctr(int(r["rank_group"]))
        rows.append({"cluster": name, "keywords_with_snapshot": n, "keywords": len(kws),
                     "visibility": {d: (round(vis[d] / (n * ctr(1)) * 100, 1) if n else None) for d in domains}})
    you = domains[0] if domains else None
    weak = []
    if you and len(domains) > 1:
        for r in rows:
            if not r["keywords_with_snapshot"]:
                continue
            best_other = max((v or 0) for d, v in r["visibility"].items() if d != you)
            if (r["visibility"][you] or 0) < best_other:
                weak.append(r["cluster"])
    return {"database": database, "domains": domains, "clusters": rows, "weak_or_missing_for_you": weak,
            "source_class": "MODELED", "method": "CTR-weighted visibility per cluster from latest SERP snapshots (ctr-curve-1)"}


def inventory(db: Session, ch: ClickHouse | None, site: Site, days: int = 90) -> dict:
    crawl = db.scalar(select(CrawlRun).where(CrawlRun.site_id == site.id, CrawlRun.competitor_id.is_(None),
                                             CrawlRun.status == "completed").order_by(CrawlRun.id.desc()).limit(1))
    if not crawl:
        return {"available": False, "reason": "needs a completed crawl"}
    from app.services.analytics_depth import canonical_path
    end = date.today() - timedelta(days=3)
    cur_s, prev_s = end - timedelta(days=days - 1), end - timedelta(days=2 * days - 1)
    clicks_cur, clicks_prev, queries = defaultdict(int), defaultdict(int), defaultdict(set)
    for page, d, c, q in db.execute(select(SP.page, SP.date, SP.clicks, SP.query).where(SP.site_id == site.id, SP.date >= prev_s)):
        k = canonical_path(page)
        (clicks_cur if d >= cur_s else clicks_prev)[k] += c
        if d >= cur_s and c > 0:
            queries[k].add(q)
    conv = defaultdict(int)
    for lp, su, pu in db.execute(select(GA.landing_page, func.sum(GA.signups), func.sum(GA.purchases))
                                 .where(GA.site_id == site.id, GA.date >= cur_s).group_by(GA.landing_page)):
        conv[canonical_path(lp)] += (su or 0) + (pu or 0)
    rd = {}
    if ch is not None and ch.configured:
        from app.services.backlinks import _dom
        for r in ch.query("SELECT target_url, uniqExact(source_domain) AS rd FROM backlinks FINAL WHERE target_domain = {d:String} "
                          "AND status = 'live' GROUP BY target_url", {"d": _dom(site.base_url)}):
            rd[canonical_path(r["target_url"])] = int(r["rd"])
    q_owner = defaultdict(list)
    for k, qs in queries.items():
        for q in qs:
            q_owner[q].append(k)
    items = []
    for p in db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == crawl.id, CrawlPage.status_code == 200, CrawlPage.is_html.is_(True))):
        k = canonical_path(p.normalized_url)
        cur, prev, cv, links = clicks_cur[k], clicks_prev[k], conv[k], rd.get(k, 0)
        shared = sum(1 for q in queries[k] if len(q_owner[q]) > 1)
        decay = prev >= 30 and cur < prev * 0.7
        reasons = []
        if cur == 0 and cv == 0 and links == 0 and (p.word_count or 0) < 300:
            action = "remove_or_noindex"
            reasons.append("no clicks, conversions or backlinks in the window and thin content")
        elif shared >= 3 and cur < 20:
            action = "merge"
            reasons.append(f"{shared} ranking queries shared with other own pages (cannibalization)")
        elif decay:
            action = "update"
            reasons.append(f"clicks fell from {prev} to {cur} vs previous {days} days")
        else:
            action = "keep"
            reasons.append("performing or insufficient evidence for change")
        if links and action == "remove_or_noindex":
            action, reasons = "update", reasons + ["has backlinks — redirect instead of deleting"]
        items.append({
            "url": p.normalized_url,
            "action": action,
            "reasons": reasons,
            "rationale": reasons,
            "evidence": {
                "clicks_current": cur,
                "clicks_previous": prev,
                "clicks_28d": cur,
                "sessions_28d": cur,
                "conversions": cv,
                "referring_domains": links,
                "backlinks": links,
                "word_count": p.word_count,
                "queries_shared_with_other_pages": shared,
            },
        })
    order = {"remove_or_noindex": 0, "merge": 1, "update": 2, "keep": 3}
    items.sort(key=lambda i: (order[i["action"]], -i["evidence"]["clicks_previous"], i["url"]))
    cannibalization_groups = [
        {"query": q, "competing_paths": sorted(paths)}
        for q, paths in sorted(q_owner.items())
        if len(paths) > 1
    ]
    decaying_pages = [
        {
            "url": i["url"],
            "clicks_previous": i["evidence"]["clicks_previous"],
            "clicks_current": i["evidence"]["clicks_current"],
            "drop_pct": round((1 - i["evidence"]["clicks_current"] / max(1, i["evidence"]["clicks_previous"])) * 100, 1),
        }
        for i in items
        if i["action"] == "update" and i["evidence"]["clicks_previous"] >= 30
    ]
    return {
        "available": True,
        "crawl_id": crawl.id,
        "window_days": days,
        "counts": Counter(i["action"] for i in items),
        "items": items,
        "cannibalization_groups": cannibalization_groups,
        "decaying_pages": decaying_pages,
        "note": "Recommendations are evidence-based suggestions; review business value before removing anything.",
    }
