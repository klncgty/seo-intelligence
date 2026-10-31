"""Module 8: Link Building & Outreach CRM.

Strict Honesty & Data Provenance Rules:
- Never fabricate or guess email addresses or contact names.
- Contact discovery only extracts verified public contact info (mailto links, visible emails on public pages,
  contact form URLs, and official social links) or explicitly marks status as NO_PUBLIC_CONTACT_FOUND.
- Outreach email generation produces user-approval-gated drafts and never sends emails without explicit user approval.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from lxml import html as lhtml

from app.core.config import get_settings
from app.infra.clickhouse import ClickHouse
from app.models import Competitor, LinkProspect, Site
from app.normalize.domains import registrable_domain
from app.services import backlinks as bl_service
from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
from app.services.crawler.parser import decode_html
from app.services.crawler.urls import host_of

EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,15}\b")
SOCIAL_HOSTS = ("linkedin.com", "twitter.com", "x.com", "instagram.com", "facebook.com")
VALID_STATUSES = {
    "PROSPECT",
    "NEW",
    "REVIEWING",
    "CONTACTED",
    "REPLIED",
    "FOLLOW_UP",
    "NEGOTIATING",
    "LINK_ACQUIRED",
    "ACQUIRED",
    "REJECTED",
}


def _prospect_out(p: LinkProspect) -> dict:
    now_iso = datetime.now(UTC).isoformat()
    col_iso = p.created_at.isoformat() if p.created_at else now_iso
    upd_iso = p.updated_at.isoformat() if p.updated_at else col_iso
    return {
        "id": p.id,
        "org_id": p.org_id,
        "site_id": p.site_id,
        "domain": p.domain,
        "source_url": p.source_url,
        "target_url": p.target_url,
        "link_type": p.link_type,
        "relevance_score": p.relevance_score,
        "domain_strength": p.domain_strength,
        "opportunity_score": p.opportunity_score,
        "contact_info": p.contact_info or {"emails": [], "contact_pages": [], "social_profiles": [], "status": "NOT_SCANNED"},
        "outreach_status": p.outreach_status,
        "email_draft": p.email_draft,
        "outreach_history": p.outreach_history or [],
        "follow_up_at": p.follow_up_at.isoformat() if p.follow_up_at else None,
        "provenance": {
            "dataSource": p.data_source,
            "data_source": p.data_source,
            "source_class": p.source_class,
            "collectedAt": col_iso,
            "collected_at": col_iso,
            "updatedAt": upd_iso,
            "updated_at": upd_iso,
            "isEstimated": False,
            "is_estimated": False,
            "confidenceScore": p.confidence_score,
            "confidence_score": p.confidence_score,
            "confidenceLevel": "HIGH" if p.confidence_score >= 0.75 else "MEDIUM",
            "confidence_level": "HIGH" if p.confidence_score >= 0.75 else "MEDIUM",
        },
    }


def extract_public_contacts_from_html(body: bytes, base_url: str, content_type: str | None = None) -> dict:
    """Extract only verified public emails, contact pages, and social profiles from HTML. Never fabricates."""
    text_html = decode_html(body or b"<html/>", content_type) or "<html/>"
    doc = lhtml.fromstring(text_html)
    emails: set[str] = set()
    contact_pages: set[str] = set()
    social_profiles: set[str] = set()

    for href in doc.xpath("//a[@href]/@href"):
        h = str(href).strip()
        if h.lower().startswith("mailto:"):
            addr = h[7:].split("?")[0].strip().lower()
            if EMAIL_RE.fullmatch(addr):
                emails.add(addr)
        elif any(k in h.lower() for k in ("/contact", "/iletisim", "/about", "/hakkimizda", "/bize-ulasin")):
            if h.startswith("/"):
                origin = "/".join(base_url.split("/")[:3])
                contact_pages.add(origin + h)
            elif h.startswith(("http://", "https://")):
                contact_pages.add(h)
        elif any(sh in h.lower() for sh in SOCIAL_HOSTS):
            social_profiles.add(h)

    visible_text = " ".join(doc.itertext())
    for m in EMAIL_RE.findall(visible_text):
        low = m.lower()
        if not low.endswith((".png", ".jpg", ".webp", ".svg", ".gif", ".js", ".css")):
            emails.add(low)

    has_any = bool(emails or contact_pages or social_profiles)
    return {
        "emails": sorted(emails),
        "contact_pages": sorted(contact_pages)[:5],
        "social_profiles": sorted(social_profiles)[:10],
        "verified_public": True,
        "fabricated": False,
        "status": "PUBLIC_CONTACT_FOUND" if has_any else "NO_PUBLIC_CONTACT_FOUND",
    }


async def discover_public_contact(db: Session, prospect: LinkProspect, transport=None) -> dict:
    s = get_settings()
    url = prospect.source_url if prospect.source_url.startswith(("http://", "https://")) else f"https://{prospect.domain}/"
    host = host_of(url)
    policy = FetchPolicy(
        allowed_hosts=[host],
        user_agent=s.crawler_user_agent,
        timeout_s=12,
        max_bytes=2_000_000,
        allow_private=s.crawler_allow_private_networks,
    )
    async with build_client(policy, transport) as client:
        res = await safe_get(client, url, policy)
    if res.status_code == 200 and res.is_html and res.body:
        info = extract_public_contacts_from_html(res.body, res.final_url or url, res.content_type)
    else:
        info = {
            "emails": [],
            "contact_pages": [],
            "social_profiles": [],
            "verified_public": True,
            "fabricated": False,
            "status": "NO_PUBLIC_CONTACT_FOUND",
        }
    prospect.contact_info = info
    db.commit()
    return _prospect_out(prospect)


def discover_prospects(
    db: Session,
    ch: ClickHouse | None,
    site: Site,
    brand_terms: list[str] | None = None,
) -> dict:
    """Discover link building prospects from Backlink Gap, Competitor Links, and Broken Links."""
    own_domain = registrable_domain(host_of(site.base_url)) or host_of(site.base_url).removeprefix("www.")
    comps = db.scalars(select(Competitor).where(Competitor.site_id == site.id, Competitor.is_active.is_(True))).all()
    comp_domains = [c.domain for c in comps]

    created = 0
    updated = 0

    def _upsert(domain: str, source_url: str, target_url: str, link_type: str, rel_score: float, ds: float | None, data_src: str):
        nonlocal created, updated
        opp_score = round(min(100.0, (ds or 35.0) * 0.55 + rel_score * 45.0), 1)
        existing = db.scalar(
            select(LinkProspect).where(
                LinkProspect.org_id == site.org_id,
                LinkProspect.source_url == source_url,
                LinkProspect.target_url == target_url,
            )
        )
        if existing is None:
            existing = LinkProspect(
                org_id=site.org_id,
                site_id=site.id,
                domain=domain,
                source_url=source_url,
                target_url=target_url,
                link_type=link_type,
                relevance_score=round(rel_score, 2),
                domain_strength=ds,
                opportunity_score=opp_score,
                contact_info={"emails": [], "contact_pages": [], "social_profiles": [], "status": "NOT_SCANNED"},
                outreach_status="PROSPECT",
                data_source=data_src,
                source_class="OBSERVED",
                confidence_score=0.8,
            )
            db.add(existing)
            created += 1
        else:
            existing.relevance_score = round(rel_score, 2)
            existing.domain_strength = ds
            existing.opportunity_score = opp_score
            updated += 1

    if ch is not None:
        # 1. Link Gap (domains linking to competitors but not own site)
        if comp_domains:
            min_c = 1 if len(comp_domains) == 1 else min(2, len(comp_domains))
            inter = bl_service.intersect(ch, own_domain, comp_domains[:5], min_competitors=min_c, limit=50)
            for p in inter.get("prospects") or []:
                dom = p["source_domain"]
                ds = float(p["domain_strength"]) if p.get("domain_strength") is not None else None
                rel = min(0.95, 0.55 + 0.15 * len(p.get("competitors_linked") or []))
                _upsert(dom, f"https://{dom}/", site.base_url, "link_gap", rel, ds, "ClickHouse_Link_Intersect")

        # 2. Broken / Lost Backlink Replacement
        lost_rows = ch.query(
            "SELECT source_domain, source_url, target_url FROM backlinks FINAL "
            "WHERE target_domain = {d:String} AND status = 'lost' LIMIT 50",
            {"d": own_domain},
        )
        ds_map = bl_service.strengths(ch, list({r["source_domain"] for r in lost_rows}))
        for r in lost_rows:
            _upsert(
                r["source_domain"],
                r["source_url"],
                r["target_url"],
                "broken_link",
                0.85,
                ds_map.get(r["source_domain"]),
                "ClickHouse_Lost_Backlinks",
            )

    db.commit()
    rows = db.scalars(
        select(LinkProspect)
        .where(LinkProspect.site_id == site.id)
        .order_by(LinkProspect.opportunity_score.desc(), LinkProspect.id.desc())
    ).all()
    return {
        "site_id": site.id,
        "created": created,
        "updated": updated,
        "total": len(rows),
        "items": [_prospect_out(r) for r in rows],
    }


def generate_outreach_draft(
    db: Session,
    prospect: LinkProspect,
    site: Site,
    custom_angle: str | None = None,
) -> dict:
    """Generate a personalized, value-oriented outreach draft gated by explicit user approval."""
    target = prospect.target_url or site.base_url
    if prospect.link_type == "broken_link":
        subject = f"{prospect.domain} üzerindeki kırık bağlantı hakkında bilgi"
        reason = f"{prospect.source_url} sayfasında yer alan bağlantının artık aktif olmadığını fark ettik."
        value_prop = f"Okuyucularınızın kesintisiz bilgiye ulaşması için güncel kaynağımızı ({target}) alternatif olarak değerlendirebilirsiniz."
    elif prospect.link_type == "unlinked_mention":
        subject = f"{site.name} referansınız için teşekkürler ({prospect.domain})"
        reason = f"{prospect.source_url} içeriğinizde markamızdan bahsettiğinizi memnuniyetle gördük."
        value_prop = f"Okuyucuların doğrudan ilgili kaynağa ulaşabilmesi adına {target} adresine kaynak bağlantısı eklemeniz faydalı olabilir."
    else:
        subject = f"{prospect.domain} okurları için tamamlayıcı kaynak önerisi: {site.name}"
        reason = f"{prospect.domain} üzerindeki sektörel rehberlerinizi inceledik."
        value_prop = custom_angle or f"Konuyla doğrudan örtüşen güncel veri ve analizlerimizi içeren {target} rehberimizi kaynak olarak paylaşmak istedik."

    body = (
        f"Merhaba {prospect.domain} Editör Ekibi,\n\n"
        f"{reason}\n\n"
        f"{value_prop}\n\n"
        f"İncelemek isterseniz bağlantı: {target}\n\n"
        f"İyi çalışmalar dileriz,\n{site.name} Ekibi"
    )
    draft = {
        "subject": subject,
        "body": body,
        "target_url": target,
        "link_type": prospect.link_type,
        "requires_user_approval": True,
        "auto_send_enabled": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    prospect.email_draft = draft
    if prospect.outreach_status == "PROSPECT":
        prospect.outreach_status = "REVIEWING"
    db.commit()
    return _prospect_out(prospect)
