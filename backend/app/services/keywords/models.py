"""Deterministic keyword models: intent, KD v1, SERP-overlap & lexical clustering, GSC volume lower bound.

All outputs are explainable (components returned) and versioned. See docs/models/kd.md and
docs/models/intent.md.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from app.normalize.text import match_key, stem_tokens

INTENT_MODEL_VERSION = "intent-rules-1"
KD_MODEL_VERSION = "kd-1"

# --------------------------------------------------------------------------- intent

MODIFIERS = {
    "transactional": {"tr": ["satın al", "satın", "fiyat", "fiyatı", "fiyatları", "ucuz", "indirim", "kampanya", "sipariş",
                             "kirala", "abonelik", "teklif", "kupon", "online al"],
                      "en": ["buy", "price", "pricing", "cheap", "discount", "deal", "order", "coupon", "subscribe", "for sale"]},
    "commercial": {"tr": ["en iyi", "karşılaştırma", "yorum", "yorumları", "inceleme", "alternatif", "vs", "önerisi",
                          "hangisi", "tavsiye", "marka"],
                   "en": ["best", "top", "review", "reviews", "vs", "versus", "compare", "comparison", "alternative",
                          "alternatives"]},
    "informational": {"tr": ["nedir", "nasıl", "neden", "ne demek", "nerede", "kaç", "rehber", "örnek", "tarifi", "anlamı"],
                      "en": ["what", "how", "why", "guide", "tutorial", "meaning", "example", "examples", "ideas"]},
    "navigational": {"tr": ["giriş", "giriş yap", "iletişim", "müşteri hizmetleri", "resmi site", "şube"],
                     "en": ["login", "sign in", "contact", "customer service", "official site"]},
}
SERP_SIGNALS = {  # SERP feature → intent it suggests
    "shopping": "transactional", "paid": "transactional", "commercial_units": "transactional",
    "local_pack": "transactional", "featured_snippet": "informational", "people_also_ask": "informational",
    "knowledge_graph": "navigational", "video": "informational", "top_stories": "informational",
}


def classify_intent(keyword: str, lang: str = "tr", serp_features: list[str] | None = None,
                    brand_terms: list[str] | None = None) -> dict:
    """Multi-intent rules. Returns intents sorted by score with the evidence used."""
    k = " " + match_key(keyword, lang) + " "
    scores: dict[str, float] = defaultdict(float)
    evidence: list[str] = []
    for intent, langs in MODIFIERS.items():
        for mod in langs.get(lang, []) + (langs["en"] if lang != "en" else []):
            if f" {match_key(mod, lang)} " in k:
                scores[intent] += 2
                evidence.append(f"modifier:{mod}")
    for term in brand_terms or []:
        if f" {match_key(term, lang)} " in k:
            scores["navigational"] += 2
            evidence.append(f"brand:{term}")
    for f in serp_features or []:
        if f in SERP_SIGNALS:
            scores[SERP_SIGNALS[f]] += 1
            evidence.append(f"serp:{f}")
    if not scores:
        return {"intents": [], "ambiguous": True, "evidence": [], "model_version": INTENT_MODEL_VERSION}
    top = max(scores.values())
    intents = [i for i, s in sorted(scores.items(), key=lambda x: (-x[1], x[0])) if s >= top * 0.5]
    return {"intents": intents, "ambiguous": len(intents) > 2, "evidence": evidence, "model_version": INTENT_MODEL_VERSION}


# --------------------------------------------------------------------------- KD v1

ORGANIC_SHRINKING = {"featured_snippet": 8, "ai_overview": 12, "local_pack": 8, "shopping": 8, "paid": 6,
                     "people_also_ask": 3, "video": 4, "top_stories": 4, "images": 2, "knowledge_graph": 5}


def keyword_difficulty(top10_domains: list[str], serp_features: list[str], domain_strength: dict[str, float] | None = None,
                       keyword: str = "", lang: str = "tr") -> dict:
    """KD v1 (0-100). Components:
    - authority (weight 0.6): mean domain strength (0-100) of top-10 — ONLY if strengths are known; else missing.
    - serp_crowding (0.25): sum of features that push organic results down, capped at 100.
    - concentration (0.15): share of top-10 held by repeated domains (brand/site dominance) → 0-100.
    Weights are renormalized over available components. Confidence reflects missing inputs.
    """
    comps: dict[str, float | None] = {}
    weights = {"authority": 0.6, "serp_crowding": 0.25, "concentration": 0.15}
    top = [d for d in top10_domains if d][:10]
    if domain_strength and top and all(d in domain_strength for d in top):
        comps["authority"] = sum(domain_strength[d] for d in top) / len(top)
    else:
        comps["authority"] = None
    comps["serp_crowding"] = float(min(100, sum(ORGANIC_SHRINKING.get(f, 0) for f in set(serp_features)) * 2))
    comps["concentration"] = (1 - len(set(top)) / len(top)) * 100 if top else None
    avail = {k: v for k, v in comps.items() if v is not None}
    if not avail or not top:
        return {"kd": None, "components": comps, "missing_inputs": [k for k, v in comps.items() if v is None],
                "confidence": 0.0, "model_version": KD_MODEL_VERSION, "note": "no SERP snapshot"}
    wsum = sum(weights[k] for k in avail)
    kd = round(sum(weights[k] * v for k, v in avail.items()) / wsum, 1)
    missing = [k for k, v in comps.items() if v is None]
    return {"kd": kd, "components": comps, "missing_inputs": missing,
            "confidence": round(sum(weights[k] for k in avail), 2), "model_version": KD_MODEL_VERSION,
            "note": "authority unknown until the backlink index / domain strength is available" if "authority" in missing else None}


# --------------------------------------------------------------------------- clustering

@dataclass
class Cluster:
    id: str
    keywords: list[str]
    shared_urls: list[str] = field(default_factory=list)
    method: str = "serp_overlap"


def serp_overlap_clusters(serps: dict[str, list[str]], min_shared: int = 3) -> list[Cluster]:
    """Union-find: keywords whose top-10 URL sets share >= min_shared URLs are joined (transitively)."""
    kws = sorted(serps)
    parent = {k: k for k in kws}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    sets = {k: set(serps[k][:10]) for k in kws}
    for i, a in enumerate(kws):
        for b in kws[i + 1:]:
            if len(sets[a] & sets[b]) >= min_shared:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, list[str]] = defaultdict(list)
    for k in kws:
        groups[find(k)].append(k)
    out = []
    for root, members in sorted(groups.items()):
        shared = set.intersection(*(sets[m] for m in members)) if len(members) > 1 else set()
        out.append(Cluster(f"serp:{match_key(root)}", sorted(members), sorted(shared)))
    return out


def _lex_key(keyword: str, lang: str) -> str:
    """TR: F5 stemming (first 5 chars of each token) — robust for agglutinative Turkish, where Snowball over-stems
    (makine → mak). EN: Snowball."""
    tokens = match_key(keyword, lang).split()
    stems = {t[:5] for t in tokens if len(t) > 2} if lang == "tr" else {s for s in stem_tokens(" ".join(tokens), lang) if len(s) > 2}
    return " ".join(sorted(stems)) or match_key(keyword, lang)


def lexical_clusters(keywords: list[str], lang: str = "tr") -> list[Cluster]:
    """Groups keywords with identical stem sets; shown separately from SERP-overlap clusters."""
    groups: dict[str, list[str]] = defaultdict(list)
    for k in keywords:
        key = _lex_key(k, lang)
        groups[key].append(k)
    return [Cluster(f"lex:{key}", sorted(m), method="lexical") for key, m in sorted(groups.items())]


# --------------------------------------------------------------------------- GSC lower bound

def gsc_volume_lower_bound(monthly_impressions: list[int]) -> dict:
    """Own GSC impressions are a LOWER BOUND of monthly searches (only counted when the site was shown).
    MODELED, tenant-only. Uses the max complete month observed."""
    if not monthly_impressions:
        return {"value": None, "source_class": "MODELED", "method": "no GSC data"}
    return {"value": max(monthly_impressions), "source_class": "MODELED", "model_version": "gsc-lower-bound-1",
            "method": "max monthly GSC impressions; true search volume is >= this value", "tenant_only": True}
