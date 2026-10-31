import asyncio
from datetime import timedelta

from sqlalchemy import select

from app import jobs
from app.db.session import SessionLocal
from app.models import Job
from app.worker import run_once

CALLS = []


@jobs.handler("test_flaky")
async def flaky(ctx, payload):
    CALLS.append(payload["n"])
    if len([c for c in CALLS if c == payload["n"]]) < payload.get("fail_times", 0) + 1:
        raise RuntimeError("transient")
    return {"ok": payload["n"]}


@jobs.handler("test_resume")
async def resumable(ctx, payload):
    start = ctx.checkpoint.get("i", 0)
    for i in range(start, 5):
        if i == 2 and not ctx.checkpoint.get("crashed"):
            await ctx.heartbeat(checkpoint={"i": i, "crashed": True})
            raise RuntimeError("crash mid-job")
        await ctx.heartbeat(checkpoint={"i": i + 1, "crashed": ctx.checkpoint.get("crashed", False)}, progress={"i": i + 1})
    return {"resumed_from": start}


def drain(db):
    while asyncio.run(run_once("w")):
        db.expire_all()


def test_idempotency_and_dag(db):
    a = jobs.enqueue(db, "noop", {"a": 1}, idempotency_key="k1")
    assert jobs.enqueue(db, "noop", {"a": 2}, idempotency_key="k1").id == a.id
    b = jobs.enqueue(db, "noop", {"b": 1})
    c = jobs.enqueue(db, "noop", {"c": 1}, depends_on=[a.id, b.id])  # fan-in
    assert c.status == "waiting"
    drain(db)
    db.expire_all()
    assert [db.get(Job, i).status for i in (a.id, b.id, c.id)] == ["succeeded"] * 3
    assert db.get(Job, c.id).finished_at >= db.get(Job, a.id).finished_at


def test_retry_backoff_and_dead_letter(db):
    ok = jobs.enqueue(db, "test_flaky", {"n": 1, "fail_times": 1}, max_attempts=3)
    dead = jobs.enqueue(db, "test_flaky", {"n": 2, "fail_times": 9}, max_attempts=2)
    child = jobs.enqueue(db, "noop", {}, depends_on=[dead.id])
    for _ in range(6):
        drain(db)
        db.execute(Job.__table__.update().where(Job.status == "failed").values(run_after=jobs.now() - timedelta(seconds=1)))
        db.commit()
    db.expire_all()
    assert db.get(Job, ok.id).status == "succeeded" and db.get(Job, ok.id).attempts == 2
    assert db.get(Job, dead.id).status == "dead" and db.get(Job, dead.id).attempts == 2
    assert db.get(Job, child.id).status == "cancelled"  # dependency failed → not run
    assert jobs.retry(db, dead.id).status == "queued"
    assert 2.5 <= jobs.backoff_s(1) <= 7.5 and jobs.backoff_s(30) <= 3600 * 1.5


def test_checkpoint_resume_after_crash(db):
    j = jobs.enqueue(db, "test_resume", {})
    for _ in range(3):
        drain(db)
        db.execute(Job.__table__.update().where(Job.status == "failed").values(run_after=jobs.now() - timedelta(seconds=1)))
        db.commit()
    db.expire_all()
    job = db.get(Job, j.id)
    assert job.status == "succeeded" and job.result == {"resumed_from": 2} and job.progress == {"i": 5}


def test_expired_lease_is_reclaimed_and_skip_locked(db):
    j = jobs.enqueue(db, "noop", {})
    with SessionLocal() as s1:
        claimed = jobs.claim(s1, "w1")
        assert claimed.id == j.id
    with SessionLocal() as s2:
        assert jobs.claim(s2, "w2") is None  # leased by w1
        s2.execute(Job.__table__.update().values(lease_until=jobs.now() - timedelta(seconds=1)))
        s2.commit()
        again = jobs.claim(s2, "w2")
        assert again.id == j.id and again.attempts == 2


def test_tenant_fairness(db):
    for i in range(5):
        jobs.enqueue(db, "noop", {}, tenant_id=1, priority=1)
    small = jobs.enqueue(db, "noop", {}, tenant_id=2, priority=100)
    with SessionLocal() as s:
        first = jobs.claim(s, "w")  # tenant 1 (higher priority)
        assert first.tenant_id == 1
        second = jobs.claim(s, "w")  # tenant 1 already has a running job → tenant 2 goes next
        assert second.id == small.id


def test_cancel_running_and_queued(db):
    q = jobs.enqueue(db, "noop", {})
    assert jobs.cancel(db, q.id).status == "cancelled"
    r = jobs.enqueue(db, "noop", {})
    with SessionLocal() as s:
        jobs.claim(s, "w")
    db.expire_all()
    assert jobs.cancel(db, r.id).cancel_requested is True
    assert asyncio.run(jobs.run(r.id)) == "cancelled"


def test_scheduler_leader_and_idempotent_ticks(db):
    jobs.upsert_schedule(db, "nightly", "noop", 3600, start_at=jobs.now() - timedelta(hours=3))
    with SessionLocal() as leader, SessionLocal() as other:
        leader.execute(select(1))
        leader.execute(jobs.text("SELECT pg_advisory_xact_lock(:k)"), {"k": jobs.SCHEDULER_LOCK})
        assert jobs.scheduler_tick(other) is None  # not leader while lock held
        leader.rollback()
    created = jobs.scheduler_tick(db)
    assert len(created) == 1  # missed intervals collapsed, no burst
    assert jobs.scheduler_tick(db) == []  # next_run_at moved forward
