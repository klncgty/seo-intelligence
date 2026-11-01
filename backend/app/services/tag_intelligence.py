"""Tag and Competitor Intelligence Engine.

Reverse-engineers top competitor on-page tags (Title, Meta Description,
Headings H1-H3, OpenGraph, Schema.org types, and TF-IDF semantic keywords)
and produces actionable blueprints for ranking #1 in Google and converting visitors.
"""

from __future__ import annotations

import re
from collections import Counter

from lxml import html as lhtml

from app.core.config import get_settings
from app.normalize.text import EN_STOP, TR_STOP, match_key
from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
from app.services.crawler.parser import decode_html, extract_structured_data
from app.services.crawler.urls import host_of

TOKEN = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def _tokens(text: str, lang: str = "tr") -> list[str]:
    stop = {match_key(w, lang) for w in (TR_STOP | EN_STOP)}
    return [t for t in TOKEN.findall(match_key(text, lang)) if t not in stop]


def analyze_page_tags(body: bytes, url: str, lang: str = "tr", content_type: str | None = None) -> dict:
    """Extract and analyze all on-page SEO tags, headings, schema, and semantics."""
    raw_html = decode_html(body or b"<html/>", content_type) or "<html/>"
    try:
        doc = lhtml.fromstring(raw_html)
    except Exception:
        doc = lhtml.fromstring("<html/>")

    # Structured Data
    sd = extract_structured_data(doc)
    schema_types = sorted({i.get("type") for i in sd if i.get("type")})

    # Title
    title_nodes = doc.xpath("//title/text()")
    title = " ".join(str(t).strip() for t in title_nodes).strip()[:300] if title_nodes else ""

    # Meta Description
    meta_desc_nodes = doc.xpath(
        "//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='description']/@content"
    )
    meta_description = " ".join(str(m).strip() for m in meta_desc_nodes).strip()[:500] if meta_desc_nodes else ""

    # Meta Keywords & Article Tags
    meta_kw_nodes = doc.xpath(
        "//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='keywords']/@content"
    )
    meta_keywords = [k.strip() for k in (str(meta_kw_nodes[0]).split(",") if meta_kw_nodes else []) if k.strip()]

    article_tags = [
        str(t).strip()
        for t in doc.xpath("//meta[@property='article:tag']/@content | //meta[@name='news_keywords']/@content")
        if str(t).strip()
    ]

    # OpenGraph Tags
    og_title_nodes = doc.xpath("//meta[@property='og:title']/@content")
    og_title = str(og_title_nodes[0]).strip() if og_title_nodes else ""
    og_desc_nodes = doc.xpath("//meta[@property='og:description']/@content")
    og_description = str(og_desc_nodes[0]).strip() if og_desc_nodes else ""
    og_type_nodes = doc.xpath("//meta[@property='og:type']/@content")
    og_type = str(og_type_nodes[0]).strip() if og_type_nodes else ""

    # Canonical
    canonical_nodes = doc.xpath("//link[@rel='canonical']/@href")
    canonical = str(canonical_nodes[0]).strip() if canonical_nodes else ""

    # Images & Alt
    img_elements = doc.xpath("//img")
    images_count = len(img_elements)
    images_with_alt = sum(1 for img in img_elements if (img.get("alt") or "").strip())
    missing_alt = images_count - images_with_alt

    # Headings (preserve hierarchy)
    headings = []
    for h in doc.xpath("//h1 | //h2 | //h3"):
        text = " ".join(h.itertext()).strip()
        if text:
            headings.append({
                "level": int(h.tag[1]),
                "tag": h.tag.upper(),
                "text": text[:200],
            })

    # Clean body text for readability and TF-IDF terms
    for bad in doc.xpath("//script | //style | //noscript | //nav | //footer | //header | //aside"):
        try:
            bad.drop_tree()
        except Exception:
            pass

    full_text = " ".join(doc.itertext())
    words = full_text.split()
    word_count = len(words)

    tokens = _tokens(full_text, lang)
    bigrams = [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    trigrams = [f"{a} {b} {c}" for a, b, c in zip(tokens, tokens[1:], tokens[2:])]
    terms_counter = Counter(tokens + bigrams + trigrams)

    return {
        "url": url,
        "title": title,
        "title_length": len(title),
        "meta_description": meta_description,
        "meta_description_length": len(meta_description),
        "meta_keywords": meta_keywords,
        "article_tags": article_tags,
        "canonical": canonical,
        "open_graph": {
            "title": og_title,
            "description": og_description,
            "type": og_type,
        },
        "headings": headings[:80],
        "h1": [h["text"] for h in headings if h["level"] == 1],
        "h2": [h["text"] for h in headings if h["level"] == 2],
        "h3": [h["text"] for h in headings if h["level"] == 3],
        "schema_types": schema_types,
        "schema_count": len(sd),
        "word_count": word_count,
        "images_count": images_count,
        "images_missing_alt": missing_alt,
        "terms": terms_counter,
    }


def compare_tags(
    target: dict,
    competitors: list[dict],
    target_keyword: str,
    lang: str = "tr",
) -> dict:
    """Compare target site against competitors and generate winning tag blueprint."""
    n_comps = len(competitors)
    if not n_comps:
        return {"error": "at least one competitor required for comparative tag intelligence"}

    norm_kw = match_key(target_keyword, lang)

    # 1. Competitor Title & Meta Analysis
    comp_titles = [c["title"] for c in competitors if c.get("title")]
    comp_title_lens = [len(t) for t in comp_titles] if comp_titles else [0]
    avg_title_len = round(sum(comp_title_lens) / max(1, len(comp_title_lens)), 1)

    comp_descs = [c["meta_description"] for c in competitors if c.get("meta_description")]
    comp_desc_lens = [len(d) for d in comp_descs] if comp_descs else [0]
    avg_desc_len = round(sum(comp_desc_lens) / max(1, len(comp_desc_lens)), 1)

    comp_h1s = [h for c in competitors for h in c.get("h1", [])]
    comp_h2s = [h for c in competitors for h in c.get("h2", [])]
    comp_h3s = [h for c in competitors for h in c.get("h3", [])]

    # Schema Consensus
    schema_counts = Counter(st for c in competitors for st in c.get("schema_types", []))
    schema_consensus = [
        {"schema": st, "count": cnt, "percentage": round((cnt / n_comps) * 100, 1)}
        for st, cnt in schema_counts.most_common()
    ]
    target_schemas = set(target.get("schema_types", []))
    missing_schemas = [
        s for s in schema_consensus if s["schema"] not in target_schemas and s["percentage"] >= 30.0
    ]

    # Semantic Terms (TF-IDF across competitor pages)
    doc_freq = Counter()
    term_totals = Counter()
    for c in competitors:
        c_terms = set(c.get("terms", {}).keys())
        for t in c_terms:
            doc_freq[t] += 1
        for t, cnt in c.get("terms", {}).items():
            term_totals[t] += cnt

    target_terms = set(target.get("terms", {}).keys())
    semantic_benchmark = []
    missing_semantic_terms = []

    for term, df in doc_freq.most_common(60):
        if len(term) < 3 or term.isnumeric():
            continue
        share = round((df / n_comps) * 100, 1)
        present_in_target = term in target_terms
        item = {
            "term": term,
            "competitor_pages_using": df,
            "competitor_consensus_pct": share,
            "in_target_page": present_in_target,
            "total_occurrences": term_totals[term],
        }
        semantic_benchmark.append(item)
        if not present_in_target and share >= 40.0:
            missing_semantic_terms.append(item)

    # Question Headings & Topic Patterns
    q_words = {"nasıl", "nedir", "ne", "hangi", "fiyatı", "fiyatları", "özellikleri", "nerede", "kaç", "how", "what", "which", "why", "best"}
    competitor_questions = []
    for h in comp_h2s + comp_h3s:
        tokens_in_h = set(match_key(h, lang).split())
        if "?" in h or bool(tokens_in_h & q_words):
            competitor_questions.append(h)
    competitor_questions = sorted(set(competitor_questions), key=lambda x: -len(x))[:12]

    # Heading Gap Analysis
    target_headings_text = " ".join([h["text"].lower() for h in target.get("headings", [])])
    missing_questions = [
        q for q in competitor_questions if not any(w in target_headings_text for w in q.lower().split()[:3])
    ]

    # On-Page Tag Quality Scores for Target
    title_has_kw = norm_kw in match_key(target.get("title", ""), lang)
    desc_has_kw = norm_kw in match_key(target.get("meta_description", ""), lang)
    h1_has_kw = any(norm_kw in match_key(h, lang) for h in target.get("h1", []))

    # Generate Recommended Winning Tags (Bilingual TR & EN)
    if lang == "en":
        rec_title = f"{target_keyword.title()} — #1 Platform for Growth"[:58]
        if len(rec_title) < 50:
            rec_title = f"{target_keyword.title()} — Discover & Convert Leads Fast"[:60]

        top_missing_kw = [t["term"] for t in missing_semantic_terms[:3]]
        kw_suffix = f" with {', '.join(top_missing_kw)}" if top_missing_kw else ""
        rec_desc = (
            f"The leading {target_keyword.lower()} for modern teams{kw_suffix}. "
            f"Automate discovery, personalize outreach at scale, and drive revenue. Try it free today."
        )[:155]

        rec_h1 = f"{target_keyword.title()}: High-Converting Outreach & Discovery"
        rec_h2 = []
        if missing_questions:
            rec_h2.extend(missing_questions[:4])
        else:
            rec_h2.extend([
                f"What is {target_keyword.title()} and How Does It Work?",
                "Key Features & Deliverability Checklist",
                "Pricing, ROI & Campaign Automation",
                f"Frequently Asked Questions About {target_keyword.title()}",
            ])
    else:
        rec_title = f"{target_keyword.title()} — Doğrudan Üreticisinden En İyisi"[:58]
        if len(rec_title) < 50:
            rec_title = f"{target_keyword.title()} — %100 Doğal & Orijinal | Hemen İncele"[:60]

        top_missing_kw = [t["term"] for t in missing_semantic_terms[:3]]
        kw_suffix = f" {', '.join(top_missing_kw)} özellikleri" if top_missing_kw else ""
        rec_desc = (
            f"{target_keyword.title()} arayanlar için en kaliteli seçenekler.{kw_suffix}. "
            f"Doğal üretim, uygun fiyat ve hızlı teslimat avantajıyla keşfedin."
        )[:155]

        rec_h1 = f"{target_keyword.title()}: Doğal ve Kusursuz Seçenekler"
        rec_h2 = []
        if missing_questions:
            rec_h2.extend(missing_questions[:4])
        else:
            rec_h2.extend([
                f"{target_keyword.title()} Nedir ve Nasıl Seçilir?",
                f"En İyi {target_keyword.title()} Çeşitleri ve Özellikleri",
                f"{target_keyword.title()} Fiyatları ve Satın Alma Rehberi",
                f"{target_keyword.title()} Kullanımı ve Saklama Koşulları",
            ])

    # Recommended Schema Blueprint
    rec_schemas = ["Organization", "BreadcrumbList"]
    if any(s["schema"] == "Product" for s in schema_consensus) or "urun" in target.get("url", ""):
        rec_schemas.append("Product")
        rec_schemas.append("Offer")
    if any(s["schema"] == "FAQPage" for s in schema_consensus) or len(competitor_questions) >= 2:
        rec_schemas.append("FAQPage")
    if not any(s in rec_schemas for s in ("Product", "FAQPage")):
        rec_schemas.append("Article")

    return {
        "target_url": target.get("url"),
        "target_keyword": target_keyword,
        "competitors_analyzed": n_comps,
        "competitor_urls": [c.get("url") for c in competitors],
        "audit": {
            "target_title": target.get("title"),
            "target_title_length": target.get("title_length"),
            "target_meta_description": target.get("meta_description"),
            "target_meta_description_length": target.get("meta_description_length"),
            "target_h1": target.get("h1"),
            "target_schemas": list(target_schemas),
            "keyword_in_title": title_has_kw,
            "keyword_in_meta_description": desc_has_kw,
            "keyword_in_h1": h1_has_kw,
            "word_count": target.get("word_count"),
            "images_missing_alt": target.get("images_missing_alt"),
        },
        "competitor_benchmarks": {
            "avg_title_length": avg_title_len,
            "avg_meta_description_length": avg_desc_len,
            "avg_word_count": round(sum(c.get("word_count", 0) for c in competitors) / n_comps, 1),
            "common_schemas": schema_consensus,
            "competitor_questions": competitor_questions,
        },
        "gaps": {
            "missing_schemas": missing_schemas,
            "missing_questions": missing_questions[:6],
            "missing_semantic_terms": missing_semantic_terms[:25],
        },
        "winning_tag_blueprint": {
            "recommended_title": rec_title,
            "recommended_meta_description": rec_desc,
            "recommended_h1": rec_h1,
            "recommended_h2_headings": rec_h2,
            "recommended_schema_types": sorted(set(rec_schemas)),
            "must_have_focus_terms": [
                {"term": s["term"], "competitor_consensus": f"%{s['competitor_consensus_pct']}", "status": "EKSİK" if not s["in_target_page"] else "MEVCUT"}
                for s in semantic_benchmark[:15]
            ],
            "rich_snippet_strategy": (
                "Google'da rakiplerden 2 kat fazla SERP alanı kaplamak için: "
                "1) FAQPage JSON-LD ile SSS sorularını snippet olarak ekleyin. "
                "2) E-ticaret ise Product + AggregateRating yıldız puanı etiketlerini yerleştirin."
            ),
        },
    }


async def run_tag_intelligence(
    target_url: str,
    competitor_urls: list[str],
    target_keyword: str,
    lang: str = "tr",
    transport=None,
) -> dict:
    """Fetch all pages and run full tag intelligence analysis."""
    s = get_settings()
    all_urls = [target_url] + competitor_urls
    fetched = {}

    for u in all_urls:
        host = host_of(u)
        policy = FetchPolicy(
            allowed_hosts=[host],
            user_agent=s.crawler_user_agent,
            timeout_s=15,
            max_bytes=3_000_000,
            allow_private=s.crawler_allow_private_networks,
        )
        try:
            async with build_client(policy, transport) as client:
                res = await safe_get(client, u, policy)
                if res.status_code == 200 and res.body:
                    fetched[u] = (res.body, res.content_type)
        except Exception:
            continue

    if target_url not in fetched:
        return {
            "error": f"target URL could not be fetched: {target_url}",
            "successful_fetches": list(fetched.keys()),
        }

    target_body, target_ct = fetched[target_url]
    target_analysis = analyze_page_tags(target_body, target_url, lang, target_ct)

    comp_analyses = []
    for u in competitor_urls:
        if u in fetched:
            b, ct = fetched[u]
            comp_analyses.append(analyze_page_tags(b, u, lang, ct))

    if not comp_analyses:
        return {
            "error": "none of the provided competitor URLs could be fetched",
            "target_analysis": target_analysis,
        }

    return compare_tags(target_analysis, comp_analyses, target_keyword, lang)
