"""Content intelligence, AI visibility and Local SEO API (spec §12–§14)."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, get_or_404
from app.api.v1.domains import _domain
from app.api.v1.keywords import _ch, _ctx
from app.auth import Principal, current_principal, get_db
from app.infra.clickhouse import ClickHouse
from app.models import AiPrompt, AiPromptSet, Competitor, Project, Site
from app.normalize.locale import DEFAULT_DATABASE
from app.services import ai_visibility as aiv
from app.services import content, credits, llm_assistant, local_seo, tag_intelligence
from app.services.keywords import service as kw_service

router = APIRouter()


class BriefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=300)
    database: str = DEFAULT_DATABASE
    top_n: int = Field(10, ge=3, le=10)
    url: str | None = Field(None, max_length=2048)
    draft: str | None = Field(None, max_length=200_000)
    site_id: int | None = None


@router.post("/content/brief")
async def brief(body: BriefIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    _ctx(body.database)
    return await content.brief(_ch(), body.keyword, body.database, body.top_n, db=db, org_id=p.org_id, site_id=body.site_id)


@router.post("/content/check")
async def check(body: BriefIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    if not body.url:
        raise ApiError(422, "validation_error", "url required")
    _ctx(body.database)
    return await content.check_page(_ch(), body.url, body.keyword, body.database, db=db, org_id=p.org_id, site_id=body.site_id)


class TagIntelligenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=300)
    url: str | None = Field(None, max_length=2048)
    competitor_urls: list[str] = Field(default_factory=list)
    site_id: int | None = None
    lang: str = "tr"


@router.post("/content/tag-intelligence")
async def get_tag_intelligence(
    body: TagIntelligenceIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(current_principal),
):
    target_url = body.url
    comp_urls = list(body.competitor_urls)

    if body.site_id is not None:
        site = get_or_404(db, Site, body.site_id, p.org_id)
        if not target_url:
            target_url = site.base_url
        if not comp_urls:
            comps = db.scalars(
                select(Competitor).where(
                    Competitor.site_id == site.id,
                    Competitor.is_active.is_(True),
                )
            ).all()
            comp_urls = [c.base_url for c in comps if c.base_url]

    if not target_url:
        raise ApiError(422, "validation_error", "target 'url' or valid 'site_id' required")
    if not comp_urls:
        raise ApiError(
            422,
            "validation_error",
            "at least one competitor URL required (via competitor_urls or registered site competitors)",
        )

    res = await tag_intelligence.run_tag_intelligence(
        target_url=target_url,
        competitor_urls=comp_urls,
        target_keyword=body.keyword,
        lang=body.lang,
    )
    if "error" in res and not res.get("audit"):
        raise ApiError(400, "analysis_failed", res["error"])
    return res


@router.get("/content/templates")
def list_templates(site_id: int | None = None, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    from app.models import ContentTemplateRecord
    stmt = select(ContentTemplateRecord).where(ContentTemplateRecord.org_id == p.org_id).order_by(ContentTemplateRecord.id.desc())
    if site_id is not None:
        stmt = stmt.where(ContentTemplateRecord.site_id == site_id)
    rows = db.scalars(stmt.limit(50)).all()
    return {
        "items": [
            {
                "id": r.id,
                "site_id": r.site_id,
                "keyword": r.keyword,
                "database": r.database,
                "target_url": r.target_url,
                "template_data": r.template_data,
                "onpage_check": r.onpage_check,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post("/content/score")
async def score(body: BriefIn, _: Principal = Depends(current_principal)):
    if not body.draft:
        raise ApiError(422, "validation_error", "draft required")
    b = await content.brief(_ch(), body.keyword, body.database, body.top_n)
    return content.coverage_score(body.draft, b, body.database.split("-")[0])


class TopicIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clusters: dict[str, list[str]] = Field(min_length=1, max_length=200)
    domains: list[str] = Field(min_length=1, max_length=5)
    database: str = DEFAULT_DATABASE


@router.post("/content/topical-map")
def topical_map(body: TopicIn, _: Principal = Depends(current_principal)):
    _ctx(body.database)
    return content.topical_map(_ch(), {k: v[:200] for k, v in body.clusters.items()}, body.database, [_domain(d) for d in body.domains])


@router.get("/sites/{site_id}/content-inventory")
def inventory(site_id: int, db: Session = Depends(get_db)):
    ch = ClickHouse()
    return content.inventory(db, ch if ch.configured else None, get_or_404(db, Site, site_id, "site"))


# ---------------------------------------------------------------- AI visibility

class PromptSetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    own_domain: str
    brand_terms: list[str] = Field(min_length=1, max_length=20)
    competitors: list[dict] = Field(default_factory=list, max_length=10)
    engines: list[str] = Field(min_length=1, max_length=4)
    samples_per_prompt: int = Field(3, ge=1, le=10)
    database: str = DEFAULT_DATABASE
    prompts: list[dict] = Field(min_length=1, max_length=200)  # [{text, category}]


@router.get("/ai-visibility/engines")
def engines(_: Principal = Depends(current_principal)):
    return {"engines": aiv.engines_status(), "warning": aiv.API_WARNING}


@router.get("/ai-visibility/sets")
def list_sets(db: Session = Depends(get_db)):
    rows = db.scalars(select(AiPromptSet).order_by(AiPromptSet.id.desc())).all()
    return {
        "items": [
            {
                "id": s.id,
                "name": s.name,
                "own_domain": s.own_domain,
                "database": s.database,
                "brand_terms": s.brand_terms,
                "engines": s.engines,
                "samples_per_prompt": s.samples_per_prompt,
            }
            for s in rows
        ]
    }


@router.post("/ai-visibility/sets", status_code=201)
def create_set(body: PromptSetIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    if any(e not in aiv.ENGINES for e in body.engines):
        raise ApiError(422, "validation_error", f"engines must be in {list(aiv.ENGINES)}")
    pid = db.scalar(select(Project.id).order_by(Project.id).limit(1))
    ps = AiPromptSet(org_id=p.org_id, project_id=pid, name=body.name, database=body.database, own_domain=_domain(body.own_domain),
                     brand_terms=body.brand_terms, engines=body.engines, samples_per_prompt=body.samples_per_prompt,
                     competitors=[{"name": str(c.get("name", ""))[:100], "domain": _domain(c["domain"]) if c.get("domain") else "",
                                   "terms": [str(t)[:100] for t in c.get("terms", [])][:10]} for c in body.competitors])
    db.add(ps)
    db.flush()
    for pr in body.prompts:
        db.add(AiPrompt(org_id=p.org_id, set_id=ps.id, text=str(pr.get("text", ""))[:2000],
                        category=pr.get("category") if pr.get("category") in ("brand", "category", "comparison", "purchase") else "category"))
    db.commit()
    n = len(body.prompts)
    return {"id": ps.id, "runs_per_execution": aiv.estimate_runs(ps, n),
            "preflight": credits.preflight(db, p.org_id, "ai_prompt_run", aiv.estimate_runs(ps, n))}


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


@router.post("/ai-visibility/sets/{set_id}/run")
async def run(set_id: int, body: RunIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ps = get_or_404(db, AiPromptSet, set_id, "prompt set")
    n = db.scalar(select(func.count()).select_from(AiPrompt).where(AiPrompt.set_id == ps.id))
    try:
        kw_service._charge_or_preflight(db, p.org_id, "ai_prompt_run", aiv.estimate_runs(ps, n), body.confirm, f"ai:{ps.id}:{uuid.uuid4().hex}")
    except kw_service.ConfirmationRequired as exc:
        raise ApiError(428, "confirmation_required", "re-send with confirm=true", exc.preflight) from exc
    except credits.InsufficientCredits as exc:
        raise ApiError(402, "insufficient_credits", str(exc)) from exc
    return await aiv.run_set(db, _ch(), ps, p.org_id)


@router.get("/ai-visibility/sets/{set_id}/report")
def report(set_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ps = get_or_404(db, AiPromptSet, set_id, "prompt set")
    prompts = {x.id: x for x in db.scalars(select(AiPrompt).where(AiPrompt.set_id == ps.id))}
    return aiv.report(_ch(), p.org_id, ps, prompts)


@router.post("/ai-visibility/sets/{set_id}/opportunities")
def ai_opps(set_id: int, site_id: int, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    ps = get_or_404(db, AiPromptSet, set_id, "prompt set")
    get_or_404(db, Site, site_id, "site")
    return aiv.citation_gap_opportunities(db, _ch(), ps, site_id, p.org_id)


# ---------------------------------------------------------------- Local SEO

@router.get("/local/gbp")
def gbp(_: Principal = Depends(current_principal)):
    return local_seo.GBP_STATUS


class NapIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=3, max_length=500)
    phone: str = Field(min_length=5, max_length=40)
    citation_urls: list[str] = Field(min_length=1, max_length=50)


@router.post("/local/nap-check")
async def nap(body: NapIn, _: Principal = Depends(current_principal)):
    from app.schemas import _validate_base_url
    try:
        urls = [_validate_base_url(u) for u in body.citation_urls]
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    return await local_seo.nap_check(body.name, body.address, body.phone, urls)


# ---------------------------------------------------------------- Flagged AI / LLM layer (§16)

@router.get("/ai-assistant/status")
def assistant_status(_: Principal = Depends(current_principal)):
    return llm_assistant.llm_status()


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: int
    question: str = Field(min_length=2, max_length=500)
    days: int = Field(28, ge=1, le=365)


@router.post("/ai-assistant/ask")
def assistant_ask(body: AskIn, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, body.site_id, "site")
    try:
        return llm_assistant.ask_data(db, site, body.question, body.days)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc


class SuggestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(pattern="^(title_meta|brief_summary|opportunity_explanation|intent_review|report_summary)$")
    trusted_facts: dict = Field(default_factory=dict)
    untrusted_content: str = Field("", max_length=50_000)


@router.post("/ai-assistant/suggest")
def assistant_suggest(body: SuggestIn, _: Principal = Depends(current_principal)):
    return llm_assistant.generate_suggestion(body.task, body.trusted_facts, body.untrusted_content)  # type: ignore[arg-type]


@router.get("/ai-assistant/eval")
def assistant_eval(_: Principal = Depends(current_principal)):
    return llm_assistant.run_eval_suite()
