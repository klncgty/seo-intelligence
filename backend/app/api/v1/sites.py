from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.common import ApiError, Page, get_or_404, paginate
from app.auth import get_db
from app.models import AuditFinding, CrawlPage, CrawlRun, Site
from app.schemas import CrawlCreate, SiteCreate, SiteUpdate, crawl_out, finding_out, page_summary_out, site_out
from app.services import crawl_service
from app.services.crawler.urls import host_of

router = APIRouter()


def _last_crawl(db: Session, site_id: int):
    return db.scalar(select(CrawlRun).where(CrawlRun.site_id == site_id, CrawlRun.competitor_id.is_(None)).order_by(CrawlRun.id.desc()).limit(1))


@router.post("/sites", status_code=201)
def create_site(body: SiteCreate, db: Session = Depends(get_db)):
    from app.auth import audit
    from app.models import Project
    p = db.info["principal"]
    project_id = body.project_id or db.scalar(select(Project.id).where(Project.org_id == p.org_id).order_by(Project.id).limit(1))
    if project_id is None or db.get(Project, project_id) is None:
        raise ApiError(422, "validation_error", "project not found in this organization")
    site = Site(org_id=p.org_id, project_id=project_id, name=body.name, base_url=body.base_url, allowed_hosts=body.allowed_hosts,
                gsc_property=body.gsc_property, ga4_property_id=body.ga4_property_id,
                conversion_events=body.conversion_events.model_dump(),
                crawl_settings=body.crawl_settings.model_dump(exclude_none=True), is_active=body.is_active)
    db.add(site)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "conflict", "a site with this base_url already exists") from exc
    audit(db, "site.create", f"site:{site.id}", {"base_url": site.base_url})
    db.commit()
    return site_out(site)


@router.get("/sites")
def list_sites(page: Page = Depends(), active: bool | None = None, db: Session = Depends(get_db)):
    stmt = select(Site).order_by(Site.id)
    if active is not None:
        stmt = stmt.where(Site.is_active.is_(active))
    return paginate(db, stmt, page, lambda s: site_out(s, _last_crawl(db, s.id)))


@router.get("/sites/{site_id}")
def get_site(site_id: int, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    return site_out(site, _last_crawl(db, site.id))


@router.patch("/sites/{site_id}")
def update_site(site_id: int, body: SiteUpdate, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    data = body.model_dump(exclude_unset=True)
    if "conversion_events" in data and body.conversion_events is not None:
        data["conversion_events"] = body.conversion_events.model_dump()
    if "crawl_settings" in data and body.crawl_settings is not None:
        data["crawl_settings"] = {**(site.crawl_settings or {}), **body.crawl_settings.model_dump(exclude_none=True)}
    for k, v in data.items():
        setattr(site, k, v)
    if host_of(site.base_url) not in (site.allowed_hosts or []):
        site.allowed_hosts = [host_of(site.base_url), *(site.allowed_hosts or [])]
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "conflict", "a site with this base_url already exists") from exc
    return site_out(site, _last_crawl(db, site.id))


@router.delete("/sites/{site_id}", status_code=204)
def delete_site(site_id: int, db: Session = Depends(get_db)):
    from app.auth import audit
    site = get_or_404(db, Site, site_id, "site")
    audit(db, "site.delete", f"site:{site.id}", {"base_url": site.base_url})
    db.delete(site)
    db.commit()
    return Response(status_code=204)


@router.post("/sites/{site_id}/crawls", status_code=202)
def start_crawl(site_id: int, body: CrawlCreate | None = None, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    if not site.is_active:
        raise ApiError(409, "conflict", "site is inactive")
    crawl_service.lock_site(db, site_id)
    if crawl_service.active_crawl_exists(db, site_id):
        raise ApiError(409, "conflict", "a crawl is already queued or running for this site")
    overrides = body.crawl_settings.model_dump(exclude_none=True) if body else {}
    run = CrawlRun(site_id=site_id, status="queued", config=crawl_service.effective_crawl_config(site, overrides))
    db.add(run)
    db.commit()
    return crawl_out(run)


@router.get("/sites/{site_id}/crawls")
def list_crawls(site_id: int, page: Page = Depends(), db: Session = Depends(get_db)):
    get_or_404(db, Site, site_id, "site")
    stmt = select(CrawlRun).where(CrawlRun.site_id == site_id, CrawlRun.competitor_id.is_(None)).order_by(CrawlRun.id.desc())
    return paginate(db, stmt, page, crawl_out)


@router.get("/crawls/{crawl_id}")
def get_crawl(crawl_id: int, db: Session = Depends(get_db)):
    return crawl_out(get_or_404(db, CrawlRun, crawl_id, "crawl"))


@router.post("/crawls/{crawl_id}/cancel")
def cancel_crawl(crawl_id: int, db: Session = Depends(get_db)):
    run = db.get(CrawlRun, crawl_id, with_for_update=True)
    if run is None:
        raise ApiError(404, "not_found", "crawl not found")
    if run.status in ("completed", "failed", "cancelled"):
        raise ApiError(409, "conflict", f"crawl already {run.status}")
    run.cancel_requested = True
    if run.status == "queued":
        run.status, run.finished_at = "cancelled", crawl_service.now()
    db.commit()
    return crawl_out(run)


@router.post("/crawls/{crawl_id}/reanalyze")
def reanalyze(crawl_id: int, db: Session = Depends(get_db)):
    run = get_or_404(db, CrawlRun, crawl_id, "crawl")
    if run.status not in ("completed", "cancelled"):
        raise ApiError(409, "conflict", "crawl must be completed or cancelled")
    return {"findings": crawl_service.analyze_crawl(db, crawl_id)}


@router.get("/crawls/{crawl_id}/pages")
def crawl_pages(crawl_id: int, page: Page = Depends(), q: str | None = Query(None, max_length=500),
                status_code: int | None = None, has_error: bool | None = None, db: Session = Depends(get_db)):
    get_or_404(db, CrawlRun, crawl_id, "crawl")
    stmt = select(CrawlPage).where(CrawlPage.crawl_run_id == crawl_id).order_by(CrawlPage.depth, CrawlPage.id)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(CrawlPage.normalized_url.ilike(like) | CrawlPage.title.ilike(like))
    if status_code is not None:
        stmt = stmt.where(CrawlPage.status_code == status_code)
    if has_error is not None:
        stmt = stmt.where(CrawlPage.fetch_error.is_not(None) if has_error else CrawlPage.fetch_error.is_(None))
    counts = dict(db.execute(select(AuditFinding.page_id, func.count()).where(AuditFinding.crawl_run_id == crawl_id)
                             .group_by(AuditFinding.page_id)).all())
    return paginate(db, stmt, page, lambda p: page_summary_out(p, counts.get(p.id, 0)))


@router.get("/crawls/{crawl_id}/findings")
def crawl_findings(crawl_id: int, page: Page = Depends(), severity: str | None = None, rule_id: str | None = None,
                   status: str | None = None, category: str | None = None, db: Session = Depends(get_db)):
    get_or_404(db, CrawlRun, crawl_id, "crawl")
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    stmt = (select(AuditFinding).where(AuditFinding.crawl_run_id == crawl_id)
            .order_by(case(sev_rank, value=AuditFinding.severity, else_=9), AuditFinding.rule_id, AuditFinding.url, AuditFinding.id))
    if severity:
        stmt = stmt.where(AuditFinding.severity.in_(severity.split(",")))
    if rule_id:
        stmt = stmt.where(AuditFinding.rule_id.in_(rule_id.split(",")))
    if status:
        stmt = stmt.where(AuditFinding.status.in_(status.split(",")))
    if category:
        stmt = stmt.where(AuditFinding.category.in_(category.split(",")))
    return paginate(db, stmt, page, finding_out)


@router.get("/crawls/{crawl_id}/audit-report")
def crawl_audit_report(crawl_id: int, db: Session = Depends(get_db)):
    from app.services import advanced_audit
    get_or_404(db, CrawlRun, crawl_id, "crawl")
    return advanced_audit.audit_report(db, crawl_id)


@router.patch("/findings/{finding_id}")
def update_finding_status(finding_id: int, body: dict, db: Session = Depends(get_db)):
    f = get_or_404(db, AuditFinding, finding_id, "finding")
    new_status = body.get("status")
    if new_status not in ("open", "in_progress", "fixed", "ignored"):
        raise ApiError(422, "validation_error", "status must be open, in_progress, fixed, or ignored")
    f.status = new_status
    db.commit()
    return finding_out(f)


@router.get("/crawls/{crawl_id}/summary")
def crawl_summary(crawl_id: int, db: Session = Depends(get_db)):
    run = get_or_404(db, CrawlRun, crawl_id, "crawl")
    dist = {}
    for code, err, n in db.execute(select(CrawlPage.status_code, CrawlPage.fetch_error.is_not(None), func.count())
                                   .where(CrawlPage.crawl_run_id == crawl_id).group_by(CrawlPage.status_code, CrawlPage.fetch_error.is_not(None))):
        key = str(code) if code is not None else "error"
        dist[key] = dist.get(key, 0) + n
    by_rule = [{"rule_id": r, "severity": s, "count": n} for r, s, n in db.execute(
        select(AuditFinding.rule_id, AuditFinding.severity, func.count()).where(AuditFinding.crawl_run_id == crawl_id)
        .group_by(AuditFinding.rule_id, AuditFinding.severity).order_by(func.count().desc(), AuditFinding.rule_id))]
    by_sev = {s: 0 for s in ("critical", "high", "medium", "low", "info")}
    for r in by_rule:
        by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + r["count"]
    return {"crawl": crawl_out(run), "status_distribution": dict(sorted(dist.items())),
            "findings_by_severity": by_sev, "findings_by_rule": by_rule, "total_findings": sum(by_sev.values())}


@router.get("/pages/{page_id}")
def page_detail(page_id: int, db: Session = Depends(get_db)):
    p = get_or_404(db, CrawlPage, page_id, "page")
    findings = db.scalars(select(AuditFinding).where(AuditFinding.page_id == p.id).order_by(AuditFinding.id)).all()
    out = page_summary_out(p, len(findings))
    out.update({"crawl_run_id": p.crawl_run_id, "final_url": p.final_url, "meta_description": p.meta_description,
                "meta_descriptions": p.meta_descriptions, "titles": p.titles, "canonical_urls": p.canonical_urls,
                "meta_robots": p.meta_robots, "x_robots_tag": p.x_robots_tag, "h1": p.h1, "hreflang": p.hreflang,
                "content_hash": p.content_hash, "redirect_chain": p.redirect_chain,
                "fetched_at": p.fetched_at.isoformat() if p.fetched_at else None,
                "outlinks": [{"target_url": l.target_url, "anchor_text": l.anchor_text, "rel": l.rel,
                              "is_internal": l.is_internal} for l in p.links[:1000]],
                "findings": [finding_out(f) for f in findings]})
    return out


@router.get("/crawls/{crawl_id}/diff")
def crawl_diff(crawl_id: int, db: Session = Depends(get_db)):
    from app.services import advanced_audit
    get_or_404(db, CrawlRun, crawl_id, "crawl")
    return advanced_audit.crawl_diff(db, crawl_id)


@router.get("/crawls/{crawl_id}/link-suggestions")
def link_suggestions(crawl_id: int, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    from app.services import advanced_audit
    get_or_404(db, CrawlRun, crawl_id, "crawl")
    pages = db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == crawl_id)).all()
    by_id = {p.id: p.normalized_url for p in pages}
    from app.models import PageLink
    edges = [(by_id[s], t) for s, t in db.execute(select(PageLink.source_page_id, PageLink.target_url)
             .join(CrawlPage, CrawlPage.id == PageLink.source_page_id)
             .where(CrawlPage.crawl_run_id == crawl_id, PageLink.is_internal.is_(True)))]
    pr = {p.normalized_url: p.internal_pagerank or 0 for p in pages}
    return {"items": advanced_audit.link_suggestions(pages, edges, pr, limit),
            "method": "topic similarity (title+H1 token Jaccard) × 0.6 + source internal PageRank × 0.4; targets with ≤2 inlinks"}


@router.get("/crawls/{crawl_id}/templates")
def templates(crawl_id: int, db: Session = Depends(get_db)):
    from app.services import advanced_audit
    get_or_404(db, CrawlRun, crawl_id, "crawl")
    return {"template_issues": advanced_audit.template_issues(db, crawl_id),
            "note": "Issues present on ≥80% of pages sharing a DOM template — fix once in the template."}
