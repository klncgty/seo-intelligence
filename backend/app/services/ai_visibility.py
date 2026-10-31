"""AI search visibility (spec §13) via OFFICIAL APIs only — consumer chat UIs are never scraped.

Answers are untrusted third-party text: they are only string-analyzed (brand/competitor mentions, cited URLs); nothing in
an answer is ever executed or treated as an instruction. API answers can differ from consumer apps — every result stores
engine, model id and date. Rates are reported with Wilson 95% intervals over multiple samples per prompt.
"""

import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infra.clickhouse import ClickHouse
from app.models import AiPrompt, AiPromptSet
from app.normalize.domains import registrable_domain
from app.normalize.text import match_key
from app.providers import runtime
from app.providers.base import ProviderError, ProviderTransientError
from app.services.analytics_depth import wilson

API_WARNING = "API answers may differ from what users see in consumer apps (personalization, UI-specific retrieval)."


class _Engine:
    name: str
    capabilities = ("ai_answer",)
    rate_per_s = 1.0
    supported_databases = ()
    unit_cost_usd = {"ai_answer": 0.01}  # rough per-call estimate for budget guard; verify with provider pricing

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    def is_configured(self) -> bool:
        return bool(self.key() and self.model())

    async def _post(self, url: str, json: dict, headers: dict, params: dict | None = None) -> dict:
        own = self._client is None
        c = self._client or httpx.AsyncClient(timeout=120, trust_env=False)
        try:
            r = await c.post(url, json=json, headers=headers, params=params)
        except httpx.TransportError as exc:
            raise ProviderTransientError(type(exc).__name__) from exc
        finally:
            if own:
                await c.aclose()
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderTransientError(f"{self.name} HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} HTTP {r.status_code}")
        return r.json()


class OpenAIEngine(_Engine):
    name = "openai"

    def key(self): return get_settings().openai_api_key
    def model(self): return get_settings().openai_visibility_model

    async def answer(self, prompt: str) -> dict:
        d = await self._post("https://api.openai.com/v1/responses", {"model": self.model(), "input": prompt, "tools": [{"type": "web_search"}]},
                             {"Authorization": f"Bearer {self.key()}"})
        texts, urls = [], []
        for item in d.get("output", []):
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    texts.append(c.get("text", ""))
                    urls += [a.get("url") for a in c.get("annotations", []) or [] if a.get("url")]
        return {"text": "\n".join(texts), "citations": urls, "model": d.get("model") or self.model()}


class AnthropicEngine(_Engine):
    name = "anthropic"

    def key(self): return get_settings().anthropic_api_key
    def model(self): return get_settings().anthropic_visibility_model

    async def answer(self, prompt: str) -> dict:
        d = await self._post("https://api.anthropic.com/v1/messages",
                             {"model": self.model(), "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}],
                              "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]},
                             {"x-api-key": self.key(), "anthropic-version": "2023-06-01"})
        texts, urls = [], []
        for b in d.get("content", []):
            if b.get("type") == "text":
                texts.append(b.get("text", ""))
                urls += [c.get("url") for c in b.get("citations", []) or [] if c.get("url")]
        return {"text": "\n".join(texts), "citations": urls, "model": d.get("model") or self.model()}


class GeminiEngine(_Engine):
    name = "gemini"

    def key(self): return get_settings().gemini_api_key
    def model(self): return get_settings().gemini_visibility_model

    async def answer(self, prompt: str) -> dict:
        d = await self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model()}:generateContent",
                             {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}, {}, {"key": self.key()})
        cand = (d.get("candidates") or [{}])[0]
        text = "\n".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        urls = [ch.get("web", {}).get("uri") for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks", []) if ch.get("web")]
        return {"text": text, "citations": [u for u in urls if u], "model": d.get("modelVersion") or self.model()}


class PerplexityEngine(_Engine):
    name = "perplexity"

    def key(self): return get_settings().perplexity_api_key
    def model(self): return get_settings().perplexity_visibility_model

    async def answer(self, prompt: str) -> dict:
        d = await self._post("https://api.perplexity.ai/chat/completions",
                             {"model": self.model(), "messages": [{"role": "user", "content": prompt}]},
                             {"Authorization": f"Bearer {self.key()}"})
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        urls = d.get("citations") or [r.get("url") for r in d.get("search_results", []) if r.get("url")]
        return {"text": text, "citations": urls, "model": d.get("model") or self.model()}


ENGINES = {"openai": OpenAIEngine, "anthropic": AnthropicEngine, "gemini": GeminiEngine, "perplexity": PerplexityEngine}
_OVERRIDES: dict[str, object] = {}


def engine(name: str):
    return _OVERRIDES.get(name) or ENGINES[name]()


def engines_status() -> list[dict]:
    return [{"engine": n, "configured": engine(n).is_configured(), "model": engine(n).model() or None,
             "setup": None if engine(n).is_configured() else f"set {n.upper()}_API_KEY and {n.upper()}_VISIBILITY_MODEL"} for n in ENGINES]


_POS_WORDS = {
    "best", "top", "leading", "reliable", "recommended", "trusted", "fast", "excellent", "great", "strong",
    "iyi", "en iyi", "güvenilir", "lider", "başarılı", "kaliteli", "önerilen", "hızlı", "güçlü", "avantajlı",
}
_NEG_WORDS = {
    "slow", "expensive", "broken", "poor", "unreliable", "avoid", "worst", "outdated", "buggy", "weak",
    "kötü", "pahalı", "yavaş", "sorunlu", "güvensiz", "zayıf", "eski", "yetersiz", "şikayet", "hatalı",
}


def analyze_sentiment(text: str, brand_terms: list[str] | None = None) -> dict:
    """Explainable lexicon-based TR/EN sentiment analysis on brand mention context."""
    low = match_key(text or "")
    pos_hits = sorted(w for w in _POS_WORDS if match_key(w) in low)
    neg_hits = sorted(w for w in _NEG_WORDS if match_key(w) in low)
    score = len(pos_hits) - len(neg_hits)
    label = "positive" if score > 0 else ("negative" if score < 0 else "neutral")
    return {"label": label, "score": score, "positive_terms": pos_hits, "negative_terms": neg_hits}


def analyze_answer(text: str, citations: list[str], own_domain: str, brand_terms: list[str], competitors: list[dict]) -> dict:
    """Pure string analysis of an untrusted answer."""
    low = match_key(text or "")
    def first_pos(terms):
        idx = [low.find(match_key(t)) for t in terms if t and match_key(t) in low]
        return min(idx) if idx else None
    own_terms = list(brand_terms) + [own_domain]
    positions = {"__own__": first_pos(own_terms)}
    for c in competitors:
        positions[c["name"]] = first_pos(list(c.get("terms") or []) + [c["name"], c.get("domain", "")])
    mentioned = sorted((p, n) for n, p in positions.items() if p is not None)
    rank = {n: i + 1 for i, (_, n) in enumerate(mentioned)}
    cited_domains = [registrable_domain((u.split("/") + ["", "", ""])[2]) or "" for u in citations]
    comp_domains = {c.get("domain"): c["name"] for c in competitors}
    sentiment = analyze_sentiment(text, own_terms) if positions["__own__"] is not None else None
    return {"brand_mentioned": positions["__own__"] is not None, "mention_position": rank.get("__own__"),
            "brand_cited": own_domain in cited_domains,
            "sentiment": sentiment,
            "competitor_mentions": [n for n in positions if n != "__own__" and positions[n] is not None],
            "competitors_cited": sorted({comp_domains[d] for d in cited_domains if d in comp_domains}), "cited_urls": citations[:50]}


def estimate_runs(ps: AiPromptSet, n_prompts: int) -> int:
    return n_prompts * len(ps.engines or []) * ps.samples_per_prompt


async def run_set(db: Session, ch: ClickHouse, ps: AiPromptSet, org_id: int, run_date: date | None = None) -> dict:
    prompts = db.scalars(select(AiPrompt).where(AiPrompt.set_id == ps.id).order_by(AiPrompt.id)).all()
    run_date = run_date or date.today()
    rows, errors = [], []
    for eng_name in ps.engines or []:
        eng = engine(eng_name)
        if not eng.is_configured():
            errors.append({"engine": eng_name, "error": "not configured"})
            continue
        for p in prompts:
            for i in range(ps.samples_per_prompt):
                try:
                    res = await runtime.call(db, eng, "ai_answer", {"engine": eng_name, "p": p.id, "s": i, "d": run_date.isoformat()},
                                             lambda p=p, eng=eng: eng.answer(p.text), org_id=org_id, use_cache=False, max_retries=2)
                except ProviderError as exc:
                    errors.append({"engine": eng_name, "prompt_id": p.id, "error": str(exc)[:200]})
                    continue
                d = res["data"]
                a = analyze_answer(d.get("text", ""), d.get("citations") or [], ps.own_domain, ps.brand_terms or [], ps.competitors or [])
                rows.append({"tenant_id": org_id, "project_id": ps.id, "prompt_id": p.id, "engine": eng_name,
                             "model_version": str(d.get("model") or ""), "locale": ps.database, "sample_index": i,
                             "run_date": run_date.isoformat(), "brand_mentioned": int(a["brand_mentioned"]),
                             "brand_cited": int(a["brand_cited"]), "mention_position": a["mention_position"],
                             "cited_urls": a["cited_urls"], "competitor_mentions": a["competitor_mentions"] + [f"cited:{c}" for c in a["competitors_cited"]],
                             "source_class": "AI", "cost_units": res["cost_usd"],
                             "fetched_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"), "version": time.time_ns()})
    if rows:
        ch.insert("ai_visibility_results", rows)
    return {"answers": len(rows), "errors": errors}


def report(ch: ClickHouse, org_id: int, ps: AiPromptSet, prompts: dict[int, AiPrompt]) -> dict:
    rows = ch.tenant_query("SELECT * FROM ai_visibility_results FINAL WHERE tenant_id = {tenant_id:UInt64} AND project_id = {p:UInt64} "
                           "AND run_date = (SELECT max(run_date) FROM ai_visibility_results WHERE tenant_id = {tenant_id:UInt64} "
                           "AND project_id = {p:UInt64})", org_id, {"p": ps.id})
    if not rows:
        return {"available": False, "reason": "no runs yet"}
    by_engine = defaultdict(list)
    for r in rows:
        by_engine[r["engine"]].append(r)
    out = {}
    for eng, rs in by_engine.items():
        n = len(rs)
        mentions = Counter()
        for r in rs:
            if int(r["brand_mentioned"]):
                mentions["__own__"] += 1
            for c in r["competitor_mentions"]:
                if not c.startswith("cited:"):
                    mentions[c] += 1
        total_m = sum(mentions.values())
        cats = defaultdict(lambda: [0, 0])
        for r in rs:
            cat = prompts[int(r["prompt_id"])].category if int(r["prompt_id"]) in prompts else "unknown"
            cats[cat][0] += int(r["brand_mentioned"])
            cats[cat][1] += 1
        cited = Counter(registrable_domain((u.split("/") + ["", "", ""])[2]) for r in rs for u in r["cited_urls"])
        pos = [int(r["mention_position"]) for r in rs if r["mention_position"] is not None]
        out[eng] = {"answers": n, "models": sorted({r["model_version"] for r in rs}),
                    "mention_rate": wilson(sum(int(r["brand_mentioned"]) for r in rs), n),
                    "citation_rate": wilson(sum(int(r["brand_cited"]) for r in rs), n),
                    "share_of_voice": round(mentions["__own__"] / total_m, 3) if total_m else None,
                    "competitor_mentions": {k: v for k, v in mentions.items() if k != "__own__"},
                    "avg_mention_position": round(sum(pos) / len(pos), 2) if pos else None,
                    "by_category": {c: wilson(m, t) for c, (m, t) in cats.items()}, "top_cited_domains": cited.most_common(15)}
    return {"available": True, "run_date": str(rows[0]["run_date"]), "source_class": "AI", "engines": out, "warning": API_WARNING}


def citation_gap_opportunities(db: Session, ch: ClickHouse, ps: AiPromptSet, site_id: int, org_id: int) -> dict:
    """Prompts where competitors are mentioned/cited in ≥50% of samples and the brand never is → low-confidence AI opportunity."""
    from app.models import GrowthOpportunity
    rows = ch.tenant_query("SELECT prompt_id, engine, countIf(brand_mentioned = 1) AS own, count() AS n, "
                           "countIf(length(competitor_mentions) > 0) AS comp FROM ai_visibility_results FINAL "
                           "WHERE tenant_id = {tenant_id:UInt64} AND project_id = {p:UInt64} GROUP BY prompt_id, engine", org_id, {"p": ps.id})
    prompts = {p.id: p for p in db.scalars(select(AiPrompt).where(AiPrompt.set_id == ps.id))}
    created = 0
    for r in rows:
        n, own, comp = int(r["n"]), int(r["own"]), int(r["comp"])
        if n < 3 or own > 0 or comp / n < 0.5 or int(r["prompt_id"]) not in prompts:
            continue
        key = f"ai_citation_gap|{ps.id}|{r['prompt_id']}|{r['engine']}"
        o = db.scalar(select(GrowthOpportunity).where(GrowthOpportunity.site_id == site_id, GrowthOpportunity.key == key))
        if o is None:
            o = GrowthOpportunity(site_id=site_id, key=key, status="new")
            db.add(o)
            created += 1
        text = prompts[int(r["prompt_id"])].text
        o.url, o.opportunity_type = None, "ai_citation_gap"
        o.title = f"AI answers ({r['engine']}) mention competitors but not you: “{text[:80]}”"
        o.description = f"In {comp}/{n} sampled answers competitors appeared; your brand appeared in none."
        o.evidence = {"prompt": text, "engine": r["engine"], "samples": n, "competitor_present": comp, "own_mentions": own}
        o.data_sources = [f"ai:{r['engine']}"]
        o.date_range, o.impact_estimate = None, {"metric": "ai_mentions", "value": None, "unit": "mentions", "method": "unknown"}
        o.confidence, o.effort, o.priority_score = 0.3, "medium", 6.3
        o.recommended_action = "Publish/strengthen authoritative content answering this question and earn citations from sources AI engines cite."
        o.validation_metric = "Mention/citation rate for this prompt in subsequent runs"
        o.limitations = API_WARNING + " Small samples; LLM outputs are non-deterministic."
        o.last_seen_at = datetime.now(UTC)
    db.commit()
    return {"created": created}
