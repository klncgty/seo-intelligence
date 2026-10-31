"""DB glue for crawls: job claiming, persistence sink, audit (re)analysis."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import AuditFinding, Competitor, CrawlPage, CrawlRun, PageLink, Site
from app.services.advanced_audit import extract_custom
from app.services.audit.engine import run_audit
from app.services.audit.types import AuditInput, LinkInput, PageInput
from app.services.crawler.engine import CrawlConfig, Crawler, PageRecord
from app.services.crawler.fetcher import FetchPolicy

log = logging.getLogger(__name__)
# Never crawl private/authenticated areas of competitor sites.
COMPETITOR_SKIP_PATHS = [
    r"/(login|log-in|signin|sign-in|signup|sign-up|register|logout|account|accounts|my-account|profile|dashboard|admin|wp-admin|wp-login\.php|cart|basket|checkout|order|orders|giris|uye-ol|kayit|hesabim|hesap|sepet|odeme)(/|$)",
    r"/(api|graphql)(/|$)",
]


def now() -> datetime:
    return datetime.now(UTC)


def effective_crawl_config(site, overrides: dict | None = None, competitor: bool = False) -> dict:
    s = get_settings()
    cfg = {
        "max_pages": s.crawler_default_max_pages,
        "max_depth": s.crawler_default_max_depth,
        "concurrency": s.crawler_default_concurrency,
        "request_delay_ms": s.crawler_default_request_delay_ms,
        "timeout_s": s.crawler_default_timeout_s,
    }
    cfg.update({k: v for k, v in (site.crawl_settings or {}).items() if k in cfg and v is not None})
    cfg.update({k: v for k, v in (overrides or {}).items() if k in cfg and v is not None})
    # Hard safety limits from env always win.
    cfg["max_pages"] = min(int(cfg["max_pages"]), s.crawler_max_pages_limit)
    cfg["max_depth"] = min(int(cfg["max_depth"]), s.crawler_max_depth_limit)
    cfg["concurrency"] = max(1, min(int(cfg["concurrency"]), s.crawler_max_concurrency_limit))
    cfg["request_delay_ms"] = max(int(cfg["request_delay_ms"]), s.crawler_min_request_delay_ms)
    cfg["timeout_s"] = max(1.0, min(float(cfg["timeout_s"]), 60.0))
    if not competitor and getattr(site, "verified_at", None) is None:
        # unverified domain → shallow audit (spec §9); verify via DNS TXT / meta / file / GSC for full limits
        cfg["max_pages"] = min(cfg["max_pages"], s.unverified_max_pages)
        cfg["max_depth"] = min(cfg["max_depth"], s.unverified_max_depth)
        cfg["unverified_limits"] = True
    if competitor:
        cfg["max_pages"] = min(cfg["max_pages"], s.competitor_max_pages)
        cfg["concurrency"] = min(cfg["concurrency"], s.competitor_max_concurrency)
        cfg["request_delay_ms"] = max(cfg["request_delay_ms"], s.competitor_min_request_delay_ms)
    return cfg


def claim_next_crawl(db: Session, worker_id: str) -> CrawlRun | None:
    """Atomically claim one queued crawl. FOR UPDATE SKIP LOCKED prevents double execution."""
    row = db.execute(
        select(CrawlRun).where(CrawlRun.status == "queued").order_by(CrawlRun.id)
        .limit(1).with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if row is None:
        db.rollback()
        return None
    if row.cancel_requested:
        row.status, row.finished_at = "cancelled", now()
        db.commit()
        return None
    row.status, row.started_at, row.heartbeat_at, row.worker_id = "running", now(), now(), worker_id
    db.commit()
    return row


def recover_stale_crawls(db: Session) -> int:
    """Crawls whose worker died (no heartbeat) are marked failed so they don't stay 'running' forever."""
    cutoff = now() - timedelta(seconds=get_settings().worker_stale_after_s)
    res = db.execute(
        update(CrawlRun).where(CrawlRun.status == "running", CrawlRun.heartbeat_at < cutoff)
        .values(status="failed", finished_at=now(), error_summary="worker heartbeat lost; crawl aborted")
    )
    db.commit()
    return res.rowcount or 0


class DbSink:
    def __init__(self, crawl_id: int, extractors: list | None = None):
        self.crawl_id = crawl_id
        self.extractors = extractors or []

    def _warc(self, rec: PageRecord) -> str | None:
        if not get_settings().crawler_store_warc or rec.fetch.status_code is None:
            return None
        from app.infra.storage import ObjectStore
        from app.infra.warc import response_record
        store = ObjectStore()
        if not store.configured:
            return None
        try:
            return store.put("warc", response_record(rec.url, rec.fetch.status_code, rec.fetch.headers, rec.fetch.body),
                             "warc.gz", "application/warc")
        except Exception as exc:  # archiving must never break the crawl
            log.warning("WARC archive failed: %s", type(exc).__name__)
            return None

    def _save(self, rec: PageRecord) -> None:
        f, p = rec.fetch, rec.parsed
        warc_ref = self._warc(rec)
        extracted_map = extract_custom(self.extractors, rec.fetch.body, p) if p and self.extractors else {}
        if p and p.unminified_resources:
            extracted_map = dict(extracted_map, unminified_resources=p.unminified_resources)
        with SessionLocal() as db:
            page = CrawlPage(
                crawl_run_id=self.crawl_id, url=rec.url, normalized_url=rec.normalized_url,
                final_url=f.final_url, depth=rec.depth, status_code=f.status_code,
                content_type=(f.content_type or "")[:255] or None, is_html=f.is_html,
                titles=p.titles if p else [], title=(p.titles[0] if p and p.titles else None),
                meta_descriptions=p.meta_descriptions if p else [],
                meta_description=(p.meta_descriptions[0] if p and p.meta_descriptions else None),
                canonical_urls=p.canonicals if p else [], canonical_url=(p.canonicals[0] if p and p.canonicals else None),
                meta_robots=(p.meta_robots[:512] if p and p.meta_robots else None),
                x_robots_tag=(rec.x_robots_tag or "")[:512] or None,
                h1=p.h1 if p else [], hreflang=p.hreflang if p else [],
                word_count=p.word_count if p else None, response_time_ms=f.elapsed_ms,
                content_hash=p.content_hash if p else None, redirect_chain=f.redirect_chain,
                fetch_error=f.error,
                structured_data=p.structured_data if p else [], simhash=p.simhash if p else None,
                template_sig=p.template_sig if p else None, images_missing_alt=p.images_missing_alt if p else None,
                extracted=extracted_map,
                warc_ref=warc_ref,
            )
            db.add(page)
            db.flush()
            links = [(l, True) for l in rec.internal_links] + [(l, False) for l in rec.external_links]
            db.add_all(PageLink(source_page_id=page.id, target_url=l.href, anchor_text=l.anchor_text or None,
                                rel=(l.rel or "")[:255] or None, is_internal=internal, link_position=l.position)
                       for l, internal in links)
            db.execute(update(CrawlRun).where(CrawlRun.id == self.crawl_id).values(heartbeat_at=now()))
            db.commit()

    async def save_page(self, rec: PageRecord) -> None:
        await asyncio.to_thread(self._save, rec)

    def _progress(self, d: int, c: int, f: int) -> None:
        with SessionLocal() as db:
            db.execute(update(CrawlRun).where(CrawlRun.id == self.crawl_id)
                       .values(pages_discovered=d, pages_crawled=c, pages_failed=f, heartbeat_at=now()))
            db.commit()

    async def progress(self, discovered: int, crawled: int, failed: int) -> None:
        await asyncio.to_thread(self._progress, discovered, crawled, failed)

    def _cancel(self) -> bool:
        with SessionLocal() as db:
            return bool(db.scalar(select(CrawlRun.cancel_requested).where(CrawlRun.id == self.crawl_id)))

    async def should_cancel(self) -> bool:
        return await asyncio.to_thread(self._cancel)


async def execute_crawl(crawl_id: int, transport=None) -> None:
    s = get_settings()
    with SessionLocal() as db:
        run = db.get(CrawlRun, crawl_id)
        target = db.get(Competitor, run.competitor_id) if run.competitor_id else db.get(Site, run.site_id)
        cfg, base_url, hosts = run.config, target.base_url, list(target.allowed_hosts or [])
        is_competitor = run.competitor_id is not None
        extractors = [] if is_competitor else list(getattr(target, "custom_extractors", None) or [])
    policy = FetchPolicy(allowed_hosts=hosts, user_agent=s.crawler_user_agent, timeout_s=cfg["timeout_s"],
                         max_bytes=s.crawler_max_response_bytes, max_redirects=s.crawler_max_redirects,
                         allow_private=s.crawler_allow_private_networks,
                         skip_path_patterns=COMPETITOR_SKIP_PATHS if is_competitor else [])
    from app.infra.ratelimit import TokenBucket
    crawler = Crawler(base_url, hosts, CrawlConfig(**{k: v for k, v in cfg.items() if k != "unverified_limits"}), policy, DbSink(crawl_id, extractors), transport=transport,
                      bucket=TokenBucket())
    status, error = "completed", None
    try:
        out = await crawler.run()
        status = "cancelled" if out.cancelled else "completed"
    except Exception as exc:  # whole-crawl failure; per-page failures never reach here
        log.exception("crawl %s failed", crawl_id)
        out, status, error = crawler.outcome, "failed", f"{type(exc).__name__}: {exc}"[:1000]
    with SessionLocal() as db:
        run = db.get(CrawlRun, crawl_id)
        run.status, run.finished_at, run.error_summary = status, now(), error
        run.pages_discovered, run.pages_crawled, run.pages_failed = out.pages_discovered, out.pages_crawled, out.pages_failed
        run.sitemap_found, run.sitemap_urls = out.sitemap_found, out.sitemap_urls
        run.robots_blocked_urls, run.truncated = out.robots_blocked_urls, out.truncated
        run.robots_txt = (out.robots_txt or "")[:100_000] or None
        run.config = dict(run.config or {}, llms_txt_found=out.llms_txt_found)
        db.commit()
    if status == "completed":
        with SessionLocal() as db:
            analyze_crawl(db, crawl_id)
            index_outbound_links(db, crawl_id)
            run = db.get(CrawlRun, crawl_id)
            if run and run.competitor_id is None and run.site_id:
                site = db.get(Site, run.site_id)
                if site is not None:
                    from app.services import growth_service
                    growth_service.generate_for_site(db, site)


def index_outbound_links(db: Session, crawl_id: int) -> int:
    """External links seen by our crawls become backlinks (source own_crawl, OBSERVED) for the linked domains."""
    from app.infra.clickhouse import ClickHouse
    from app.services import backlinks
    ch = ClickHouse()
    if not ch.configured:
        return 0
    rows = db.execute(select(CrawlPage.final_url, CrawlPage.normalized_url, PageLink.target_url, PageLink.anchor_text,
                             PageLink.rel, PageLink.link_position)
                      .join(CrawlPage, CrawlPage.id == PageLink.source_page_id)
                      .where(CrawlPage.crawl_run_id == crawl_id, PageLink.is_internal.is_(False))).all()
    try:
        return backlinks.ingest(ch, [{"source_url": f or n, "target_url": t, "anchor": a, "rel": r, "link_position": pos or "unknown"}
                                     for f, n, t, a, r, pos in rows], "own_crawl", "OBSERVED")
    except Exception as exc:
        log.warning("outbound link indexing failed: %s", type(exc).__name__)
        return 0


def build_audit_input(db: Session, crawl_id: int) -> tuple[AuditInput, dict[str, int]]:
    run = db.get(CrawlRun, crawl_id)
    site = db.get(Competitor, run.competitor_id) if run.competitor_id else db.get(Site, run.site_id)
    pages = db.scalars(select(CrawlPage).where(CrawlPage.crawl_run_id == crawl_id).order_by(CrawlPage.id)).all()
    page_ids = {p.normalized_url: p.id for p in pages}
    url_by_id = {p.id: p.normalized_url for p in pages}
    links = db.execute(
        select(PageLink.source_page_id, PageLink.target_url, PageLink.is_internal, PageLink.rel, PageLink.anchor_text)
        .join(CrawlPage, CrawlPage.id == PageLink.source_page_id).where(CrawlPage.crawl_run_id == crawl_id)
        .order_by(PageLink.id)
    ).all()
    llms_txt_found = bool((run.config or {}).get("llms_txt_found")) or any(
        p.normalized_url.rstrip("/").endswith("/llms.txt") for p in pages
    )
    inp = AuditInput(
        base_url=site.base_url,
        pages=[PageInput(
            url=p.normalized_url, page_id=p.id, status_code=p.status_code, content_type=p.content_type,
            is_html=p.is_html, titles=list(p.titles or []), meta_descriptions=list(p.meta_descriptions or []),
            h1=list(p.h1 or []), canonicals=[c for c in (p.canonical_urls or []) if c],
            meta_robots=p.meta_robots, x_robots_tag=p.x_robots_tag,
            hreflang=[(h.get("lang", ""), h.get("href", "")) for h in (p.hreflang or [])],
            word_count=p.word_count, fetch_error=p.fetch_error,
            redirect_chain=[(h.get("url"), h.get("status_code")) for h in (p.redirect_chain or [])],
            final_url=p.final_url, depth=p.depth,
            unminified_resources=list((p.extracted or {}).get("unminified_resources") or []),
        ) for p in pages],
        links=[LinkInput(source_url=url_by_id[s], target_url=t, is_internal=i, rel=r, anchor_text=a) for s, t, i, r, a in links],
        sitemap_urls=set(run.sitemap_urls or []),
        robots_blocked_urls=set(run.robots_blocked_urls or []),
        sitemap_found=run.sitemap_found,
        crawl_truncated=run.truncated,
        llms_txt_found=llms_txt_found,
    )
    return inp, page_ids


def finding_fingerprint(rule_id: str, url: str | None, evidence: dict) -> str:
    raw = json.dumps([rule_id, url, evidence], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def analyze_crawl(db: Session, crawl_id: int) -> int:
    """(Re)run the audit. Replaces this crawl's findings atomically → no duplicates on reanalysis."""
    from app.services import advanced_audit

    prev_statuses = dict(
        db.execute(select(AuditFinding.fingerprint, AuditFinding.status).where(AuditFinding.crawl_run_id == crawl_id)).all()
    )
    inp, page_ids = build_audit_input(db, crawl_id)
    findings = run_audit(inp)
    db.execute(delete(AuditFinding).where(AuditFinding.crawl_run_id == crawl_id))
    seen: set[str] = set()
    for f in findings:
        fp = finding_fingerprint(f.rule_id, f.url, f.evidence)
        if fp in seen:
            continue
        seen.add(fp)
        meta = advanced_audit.enrich_finding_metadata(f.rule_id, f.severity, f.title, f.description)
        db.add(AuditFinding(
            crawl_run_id=crawl_id, page_id=page_ids.get(f.url) if f.url else None, url=f.url,
            rule_id=f.rule_id, severity=f.severity, title=f.title[:300], description=f.description,
            evidence=json.loads(json.dumps(f.evidence, default=str)), remediation=f.remediation, fingerprint=fp,
            status=prev_statuses.get(fp, "open"),
            category=meta["category"],
            root_cause=meta["root_cause"],
            seo_impact=meta["seo_impact"],
            priority=meta["priority"],
        ))
    db.commit()
    advanced_audit.run(db, crawl_id)
    return len(seen)


def active_crawl_exists(db: Session, site_id: int, competitor_id: int | None = None) -> bool:
    cond = CrawlRun.competitor_id == competitor_id if competitor_id else CrawlRun.competitor_id.is_(None)
    return bool(db.scalar(select(func.count()).select_from(CrawlRun).where(
        CrawlRun.site_id == site_id, cond, CrawlRun.status.in_(("queued", "running")))))


def lock_site(db: Session, site_id: int) -> None:
    """Serialize crawl creation per site (advisory xact lock) so two requests can't both enqueue."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 1_000_000 + site_id})
