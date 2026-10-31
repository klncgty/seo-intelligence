"""Plans, limits and credits (spec §2). Append-only ledger; preflight before expensive operations.

Unit costs are platform credits (not money). Provider USD cost is recorded separately per charge so
admins can see margin. The "local" plan has no credit limit (single-user, own keys).
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CreditLedger, Membership, Organization, Project, Site

UNIT_COSTS = {  # credits per unit
    "serp_request": 1.0,
    "keyword_lookup": 0.1,
    "backlink_query": 2.0,
    "ai_prompt_run": 3.0,
    "crawled_page": 0.01,
    "export_row": 0.001,
}

PLANS = {
    "local": {"monthly_credits": None, "projects": None, "tracked_keywords": None, "crawl_pages_month": None, "users": None},
    "starter": {"monthly_credits": 5_000, "projects": 3, "tracked_keywords": 500, "crawl_pages_month": 100_000, "users": 2},
    "pro": {"monthly_credits": 50_000, "projects": 15, "tracked_keywords": 5_000, "crawl_pages_month": 1_000_000, "users": 10},
}


class InsufficientCredits(Exception):
    pass


class PlanLimitExceeded(Exception):
    pass


def plan_of(db: Session, org_id: int) -> dict:
    org = db.get(Organization, org_id)
    return {"name": org.plan, **PLANS.get(org.plan, PLANS["starter"])}


def _period_start() -> datetime:
    n = datetime.now(UTC)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def balance(db: Session, org_id: int) -> float | None:
    """Remaining credits this month; None = unlimited plan."""
    plan = plan_of(db, org_id)
    if plan["monthly_credits"] is None:
        return None
    spent_or_granted = db.scalar(select(func.coalesce(func.sum(CreditLedger.amount), 0.0)).where(
        CreditLedger.org_id == org_id, CreditLedger.created_at >= _period_start())) or 0.0
    return plan["monthly_credits"] + float(spent_or_granted)


def estimate(operation: str, quantity: int) -> float:
    if operation not in UNIT_COSTS:
        raise ValueError(f"unknown operation {operation!r}")
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    return round(UNIT_COSTS[operation] * quantity, 4)


def preflight(db: Session, org_id: int, operation: str, quantity: int) -> dict:
    cost = estimate(operation, quantity)
    bal = balance(db, org_id)
    return {"operation": operation, "quantity": quantity, "estimated_credits": cost, "balance": bal,
            "allowed": bal is None or bal >= cost, "requires_confirmation": cost > 0}


def charge(db: Session, org_id: int, operation: str, quantity: int, *, confirmed: bool,
           idempotency_key: str | None = None, provider_cost_usd: float | None = None) -> float:
    """Deduct credits. Callers must pass confirmed=True (user accepted the preflight estimate)."""
    if not confirmed:
        raise PermissionError("credit charge requires explicit confirmation of the preflight estimate")
    cost = estimate(operation, quantity)
    if idempotency_key and db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == idempotency_key)):
        return 0.0
    bal = balance(db, org_id)
    if bal is not None and bal < cost:
        raise InsufficientCredits(f"needs {cost} credits, {bal} left")
    db.add(CreditLedger(org_id=org_id, amount=-cost, operation=operation, quantity=quantity,
                        provider_cost_usd=provider_cost_usd, idempotency_key=idempotency_key))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return 0.0
    return cost


def check_limit(db: Session, org_id: int, resource: str) -> None:
    plan = plan_of(db, org_id)
    limit = plan.get(resource)
    if limit is None:
        return
    counts = {
        "projects": lambda: db.scalar(select(func.count()).select_from(Project).where(Project.org_id == org_id)),
        "users": lambda: db.scalar(select(func.count()).select_from(Membership).where(Membership.org_id == org_id)),
        "sites": lambda: db.scalar(select(func.count()).select_from(Site).where(Site.org_id == org_id)),
    }
    if resource in counts and counts[resource]() >= limit:
        raise PlanLimitExceeded(f"plan {plan['name']} allows {limit} {resource}")


def usage(db: Session, org_id: int) -> dict:
    rows = db.execute(select(CreditLedger.operation, func.sum(-CreditLedger.amount), func.sum(CreditLedger.quantity),
                             func.sum(CreditLedger.provider_cost_usd))
                      .where(CreditLedger.org_id == org_id, CreditLedger.created_at >= _period_start(),
                             CreditLedger.amount < 0).group_by(CreditLedger.operation)).all()
    return {"period_start": _period_start().isoformat(), "plan": plan_of(db, org_id), "balance": balance(db, org_id),
            "by_operation": [{"operation": o, "credits": float(c), "quantity": int(q),
                              "provider_cost_usd": float(u) if u is not None else None} for o, c, q, u in rows]}
