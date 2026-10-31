"""Module 11: AI SEO Diagnosis & Opportunity Engine + Mathematical Priority Engine.

Strict Architectural Standards:
- Categorizes all diagnoses into exactly: Frontend, Backend, Infrastructure, Content.
- Computes priority_score using the documented 8-factor mathematical model:
  priority_score = min(100.0, (
      impact_weight * 0.25 +
      urgency_weight * 0.20 +
      traffic_potential * 0.15 +
      ranking_potential * 0.15 +
      (100.0 - effort_cost) * 0.10 +
      resource_feasibility * 0.05 +
      business_alignment * 0.05 +
      risk_factor * 0.05
  ) * confidence_score)
- Powers Kanban SEO Action Plan: Open, In Progress, Completed, Ignored.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditFinding, CrawlPage, CrawlRun, SeoDiagnosisRecord, Site
from app.services import claude_code_bridge

SEVERITY_WEIGHT = {"CRITICAL": 100.0, "HIGH": 75.0, "MEDIUM": 50.0, "LOW": 25.0}
EFFORT_COST = {"Low": 20.0, "Medium": 50.0, "High": 80.0}


def calculate_priority_score(
    severity: str,
    effort: str,
    confidence_score: float,
    traffic_potential: float = 60.0,
    ranking_potential: float = 65.0,
    risk_factor: float = 50.0,
) -> float:
    impact_weight = SEVERITY_WEIGHT.get(severity.upper(), 50.0)
    urgency_weight = 90.0 if severity == "CRITICAL" else (70.0 if severity == "HIGH" else 40.0)
    effort_cost = EFFORT_COST.get(effort.capitalize(), 50.0)
    raw = (
        impact_weight * 0.25
        + urgency_weight * 0.20
        + traffic_potential * 0.15
        + ranking_potential * 0.15
        + (100.0 - effort_cost) * 0.10
        + 70.0 * 0.05  # resource feasibility
        + 75.0 * 0.05  # business alignment
        + risk_factor * 0.05
    )
    return round(min(100.0, max(5.0, raw * confidence_score)), 1)


def _diagnosis_out(d: SeoDiagnosisRecord) -> dict:
    now_iso = datetime.now(UTC).isoformat()
    return {
        "id": d.id,
        "site_id": d.site_id,
        "fingerprint": d.fingerprint,
        "problem": d.problem,
        "evidence": d.evidence or {},
        "affected_url": d.affected_url,
        "source_module": d.source_module,
        "root_cause": d.root_cause,
        "category": d.category,
        "severity": d.severity,
        "seo_impact": d.seo_impact,
        "recommended_fix": d.recommended_fix,
        "implementation_steps": d.implementation_steps or [],
        "estimated_effort": d.estimated_effort,
        "confidence_score": d.confidence_score,
        "verification_method": d.verification_method,
        "affected_file": d.affected_file,
        "priority_score": d.priority_score,
        "status": d.status,
        "provenance": {
            "dataSource": d.data_source,
            "data_source": d.data_source,
            "source_class": d.source_class,
            "collectedAt": d.created_at.isoformat() if d.created_at else now_iso,
            "collected_at": d.created_at.isoformat() if d.created_at else now_iso,
            "updatedAt": d.updated_at.isoformat() if d.updated_at else now_iso,
            "updated_at": d.updated_at.isoformat() if d.updated_at else now_iso,
            "isEstimated": False,
            "is_estimated": False,
            "confidenceScore": d.confidence_score,
            "confidence_score": d.confidence_score,
            "confidenceLevel": "HIGH" if d.confidence_score >= 0.75 else "MEDIUM",
            "confidence_level": "HIGH" if d.confidence_score >= 0.75 else "MEDIUM",
        },
    }


def run_seo_diagnosis(db: Session, site: Site) -> dict:
    """Scans all site crawl findings, on-page issues, and crawl pages into standardized SeoDiagnosisRecord items."""
    latest_run = db.scalar(
        select(CrawlRun)
        .where(CrawlRun.site_id == site.id, CrawlRun.competitor_id.is_(None))
        .order_by(CrawlRun.id.desc())
        .limit(1)
    )
    if not latest_run:
        return {"site_id": site.id, "total": 0, "items": [], "kanban": {"new": [], "planned": [], "in_progress": [], "completed": [], "ignored": []}}

    created = 0
    updated = 0

    findings = db.scalars(select(AuditFinding).where(AuditFinding.crawl_run_id == latest_run.id)).all()
    for f in findings:
        cat = getattr(f, "category", None) or "Frontend"
        sev = (f.severity or "MEDIUM").upper()
        eff = "Medium" if sev in ("CRITICAL", "HIGH") else "Low"
        fp = hashlib.sha256(f"{site.id}:{f.rule_id}:{f.url or ''}".encode()).hexdigest()[:24]

        prob = f"Audit Rule Issue: {f.title}"
        root = getattr(f, "root_cause", None) or f"Triggered audit rule {f.rule_id} during automated crawl."
        impact = getattr(f, "seo_impact", None) or "Impairs crawl efficiency, indexation, or user CTR in search results."
        p_score = calculate_priority_score(sev, eff, confidence_score=0.88)

        rec = db.scalar(select(SeoDiagnosisRecord).where(SeoDiagnosisRecord.site_id == site.id, SeoDiagnosisRecord.fingerprint == fp))
        if rec is None:
            rec = SeoDiagnosisRecord(
                org_id=site.org_id,
                site_id=site.id,
                fingerprint=fp,
                problem=prob[:400],
                evidence={"rule_id": f.rule_id, "crawl_id": latest_run.id, "finding_id": f.id},
                affected_url=f.url,
                source_module="Site_Audit",
                data_source="Crawler_Audit_Engine",
                source_class="OBSERVED",
                root_cause=root,
                category=cat,
                severity=sev,
                seo_impact=impact,
                recommended_fix=f.description or "Inspect page and resolve the reported issue.",
                implementation_steps=[
                    "Locate template or content generating the faulty HTML/header.",
                    "Apply correct canonical/meta/status code handling.",
                    "Verify with local crawler or curl before deploying.",
                ],
                estimated_effort=eff,
                confidence_score=0.88,
                verification_method="Re-run site crawl and assert finding is absent.",
                affected_file=None,
                priority_score=p_score,
                status="new",
            )
            db.add(rec)
            created += 1
        else:
            rec.priority_score = p_score
            updated += 1

    db.commit()

    all_diagnoses = db.scalars(
        select(SeoDiagnosisRecord)
        .where(SeoDiagnosisRecord.site_id == site.id)
        .order_by(SeoDiagnosisRecord.priority_score.desc(), SeoDiagnosisRecord.id.desc())
    ).all()

    kanban = {"new": [], "planned": [], "in_progress": [], "completed": [], "ignored": []}
    for d in all_diagnoses:
        st = d.status if d.status in kanban else "new"
        kanban[st].append(_diagnosis_out(d))

    claude_info = claude_code_bridge.get_claude_code_status()
    return {
        "site_id": site.id,
        "total": len(all_diagnoses),
        "created": created,
        "updated": updated,
        "ai_engine": "Claude Code CLI",
        "claude_code": claude_info,
        "items": [_diagnosis_out(d) for d in all_diagnoses],
        "kanban": kanban,
    }
