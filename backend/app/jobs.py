"""Generic Postgres job queue + DAG + scheduler (ADR 0004).

- enqueue(): idempotency_key dedupes; depends_on → status "waiting" until all deps succeed.
- claim(): FOR UPDATE SKIP LOCKED with per-tenant fairness (tenant with fewest running jobs first),
  then priority, then age. Sets a lease; expired leases are reclaimed (crashed worker).
- run(): executes a registered handler with a JobContext (heartbeat/checkpoint/progress/cancel).
  Failures retry with exponential backoff + jitter; after max_attempts → "dead" (dead-letter).
- Progress is broadcast with pg_notify('job_events', ...) for the SSE endpoint.
- Scheduler: one leader (pg_try_advisory_lock) turns due schedules into idempotent jobs.
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Job, JobDependency, Schedule

log = logging.getLogger(__name__)
LEASE_S = 120
SCHEDULER_LOCK = 724_001
HANDLERS: dict[str, Callable[["JobContext", dict], Awaitable[dict | None]]] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def now() -> datetime:
    return datetime.now(UTC)


def notify(db: Session, job: Job) -> None:
    db.execute(text("SELECT pg_notify('job_events', :p)"),
               {"p": json.dumps({"id": job.id, "status": job.status, "progress": job.progress})})


def enqueue(db: Session, kind: str, payload: dict | None = None, *, tenant_id: int | None = None, priority: int = 100,
            idempotency_key: str | None = None, depends_on: list[int] | None = None, max_attempts: int = 5,
            run_after: datetime | None = None) -> Job:
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            return existing
    from app.infra import observability as obs
    pl = dict(payload or {})
    if "_traceparent" not in pl and (tp := obs.traceparent()):
        pl["_traceparent"] = tp
    job = Job(kind=kind, payload=pl, tenant_id=tenant_id, priority=priority, idempotency_key=idempotency_key,
              max_attempts=max_attempts, status="waiting" if depends_on else "queued", run_after=run_after or now())
    db.add(job)
    try:
        db.flush()
    except IntegrityError:  # concurrent enqueue with same idempotency key
        db.rollback()
        return db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    for dep in depends_on or []:
        db.add(JobDependency(job_id=job.id, depends_on_id=dep))
    db.commit()
    return job


def _release_waiting(db: Session) -> None:
    """waiting → queued when every dependency succeeded; → cancelled if any dependency is dead/cancelled."""
    db.execute(text("""
        UPDATE jobs j SET status = 'cancelled', finished_at = now(), error = 'dependency failed'
        WHERE j.status = 'waiting' AND EXISTS (
          SELECT 1 FROM job_dependencies d JOIN jobs p ON p.id = d.depends_on_id
          WHERE d.job_id = j.id AND p.status IN ('dead', 'cancelled'))"""))
    db.execute(text("""
        UPDATE jobs j SET status = 'queued'
        WHERE j.status = 'waiting' AND NOT EXISTS (
          SELECT 1 FROM job_dependencies d JOIN jobs p ON p.id = d.depends_on_id
          WHERE d.job_id = j.id AND p.status <> 'succeeded')"""))


def claim(db: Session, worker_id: str, kinds: list[str] | None = None) -> Job | None:
    _release_waiting(db)
    # Reclaim expired leases (worker crashed mid-job) → retry later.
    db.execute(update(Job).where(Job.status == "running", Job.lease_until < now())
               .values(status="failed", error="lease expired (worker lost)", run_after=now()))
    db.commit()
    running = (select(Job.tenant_id, func.count().label("n")).where(Job.status == "running")
               .group_by(Job.tenant_id).subquery())
    stmt = (select(Job).outerjoin(running, running.c.tenant_id.is_not_distinct_from(Job.tenant_id))
            .where(Job.status.in_(("queued", "failed")), Job.run_after <= now(), Job.attempts < Job.max_attempts)
            .order_by(func.coalesce(running.c.n, 0), Job.priority, Job.run_after, Job.id)
            .limit(1).with_for_update(of=Job, skip_locked=True))
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    job = db.execute(stmt).scalar_one_or_none()
    if job is None:
        db.rollback()
        return None
    job.status, job.worker_id, job.attempts = "running", worker_id, job.attempts + 1
    job.started_at = job.started_at or now()
    job.heartbeat_at, job.lease_until = now(), now() + timedelta(seconds=LEASE_S)
    notify(db, job)
    db.commit()
    return job


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, job_id: int, checkpoint: dict | None):
        self.job_id = job_id
        self.checkpoint = checkpoint or {}

    def _update(self, **values) -> bool:
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            for k, v in values.items():
                setattr(job, k, v)
            job.heartbeat_at, job.lease_until = now(), now() + timedelta(seconds=LEASE_S)
            notify(db, job)
            db.commit()
            return job.cancel_requested

    async def heartbeat(self, checkpoint: dict | None = None, progress: dict | None = None) -> None:
        """Extend lease, persist resumable checkpoint/progress; raises JobCancelled if requested."""
        values = {}
        if checkpoint is not None:
            self.checkpoint = checkpoint
            values["checkpoint"] = checkpoint
        if progress is not None:
            values["progress"] = progress
        if await asyncio.to_thread(self._update, **values):
            raise JobCancelled()


def backoff_s(attempt: int, base: float = 5.0, cap: float = 3600.0) -> float:
    return min(cap, base * 2 ** (attempt - 1)) * random.uniform(0.5, 1.5)


async def run(job_id: int) -> str:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        kind, payload, checkpoint = job.kind, dict(job.payload), job.checkpoint
    fn = HANDLERS.get(kind)
    status, result, error = "succeeded", None, None
    from app.infra import observability as obs
    obs.start_trace(payload.pop("_traceparent", None))  # continue the API request's trace inside the worker
    _t0 = time.perf_counter()
    try:
        if fn is None:
            raise LookupError(f"no handler for job kind {kind!r}")
        result = await fn(JobContext(job_id, checkpoint), payload)
    except JobCancelled:
        status = "cancelled"
    except Exception as exc:
        log.warning("job %s (%s) failed: %s", job_id, kind, type(exc).__name__)
        status, error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
    obs.JOB_DURATION.observe(time.perf_counter() - _t0, kind=kind)
    obs.JOBS_PROCESSED.inc(kind=kind, outcome=status)
    with SessionLocal() as db:
        job = db.get(Job, job_id, with_for_update=True)
        job.error = error
        if status == "failed":
            if job.attempts >= job.max_attempts or fn is None:
                job.status, job.finished_at = "dead", now()
            else:
                job.status, job.run_after = "failed", now() + timedelta(seconds=backoff_s(job.attempts))
        else:
            job.status, job.finished_at, job.result = status, now(), result
        job.lease_until = None
        notify(db, job)
        db.commit()
        final = job.status
    return final


def cancel(db: Session, job_id: int) -> Job | None:
    job = db.get(Job, job_id, with_for_update=True)
    if job is None:
        return None
    if job.status in ("queued", "waiting", "failed"):
        job.status, job.finished_at = "cancelled", now()
    elif job.status == "running":
        job.cancel_requested = True
    notify(db, job)
    db.commit()
    return job


def retry(db: Session, job_id: int) -> Job | None:
    job = db.get(Job, job_id, with_for_update=True)
    if job is None or job.status not in ("dead", "cancelled", "failed"):
        return job
    job.status, job.attempts, job.run_after, job.error, job.cancel_requested = "queued", 0, now(), None, False
    db.commit()
    return job


# ---------------------------------------------------------------- scheduler

def upsert_schedule(db: Session, name: str, kind: str, interval_s: int, payload: dict | None = None,
                    tenant_id: int | None = None, start_at: datetime | None = None) -> Schedule:
    sch = db.scalar(select(Schedule).where(Schedule.name == name)) or Schedule(name=name, next_run_at=start_at or now())
    sch.kind, sch.interval_s, sch.payload, sch.tenant_id = kind, interval_s, payload or {}, tenant_id
    db.add(sch)
    db.commit()
    return sch


def scheduler_tick(db: Session) -> list[int] | None:
    """Enqueue due schedules. Returns None if another process is leader."""
    got = db.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SCHEDULER_LOCK})
    if not got:
        db.rollback()
        return None
    created = []
    for sch in db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at <= now())
                          .with_for_update(skip_locked=True)).all():
        key = f"schedule:{sch.name}:{int(sch.next_run_at.timestamp())}"
        job = Job(kind=sch.kind, payload=sch.payload, tenant_id=sch.tenant_id, idempotency_key=key)
        if not db.scalar(select(Job.id).where(Job.idempotency_key == key)):
            db.add(job)
            db.flush()
            created.append(job.id)
            sch.last_job_id = job.id
        while sch.next_run_at <= now():  # skip missed intervals instead of bursting
            sch.next_run_at += timedelta(seconds=sch.interval_s)
    db.commit()
    return created


@handler("noop")
async def _noop(ctx: JobContext, payload: dict) -> dict:
    await ctx.heartbeat(progress={"done": True})
    return {"echo": payload}
