"""Jobs, live progress (SSE via LISTEN/NOTIFY), readiness, metric catalog."""

import asyncio
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import jobs
from app.api.v1.common import ApiError, Page, get_or_404, paginate
from app.core.config import get_settings
from app.auth import get_db
from app.db.session import SessionLocal
from app.models import Job

router = APIRouter()


def job_out(j: Job) -> dict:
    return {"id": j.id, "tenant_id": j.tenant_id, "kind": j.kind, "status": j.status, "priority": j.priority,
            "attempts": j.attempts, "max_attempts": j.max_attempts, "progress": j.progress, "result": j.result,
            "error": j.error, "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None}


@router.get("/jobs")
def list_jobs(status: str | None = None, kind: str | None = None, page: Page = Depends(), db: Session = Depends(get_db)):
    stmt = select(Job).order_by(Job.id.desc())
    if status:
        stmt = stmt.where(Job.status.in_(status.split(",")))
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return paginate(db, stmt, page, job_out)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    return job_out(get_or_404(db, Job, job_id, "job"))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    j = jobs.cancel(db, job_id)
    if j is None:
        raise ApiError(404, "not_found", "job not found")
    return job_out(j)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, db: Session = Depends(get_db)):
    j = jobs.retry(db, job_id)
    if j is None:
        raise ApiError(404, "not_found", "job not found")
    return job_out(j)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: int, timeout_s: int = Query(300, ge=1, le=3600)):
    """Server-Sent Events: emits the job state on every NOTIFY until it reaches a terminal state."""
    import psycopg

    with SessionLocal() as db:
        if db.get(Job, job_id) is None:
            raise ApiError(404, "not_found", "job not found")
    dsn = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")

    def state() -> dict:
        with SessionLocal() as db:
            return job_out(db.get(Job, job_id))

    async def stream():
        terminal = {"succeeded", "dead", "cancelled"}
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute("LISTEN job_events")
            cur = await asyncio.to_thread(state)
            yield f"event: state\ndata: {json.dumps(cur)}\n\n"
            if cur["status"] in terminal:
                return
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            gen = conn.notifies()
            while loop.time() < deadline:
                try:
                    note = await asyncio.wait_for(anext(gen), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if json.loads(note.payload).get("id") != job_id:
                    continue
                cur = await asyncio.to_thread(state)
                yield f"event: state\ndata: {json.dumps(cur)}\n\n"
                if cur["status"] in terminal:
                    return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/metrics/catalog")
def metric_catalog():
    from app.semantic import catalog
    return {"metrics": catalog()}


def readiness() -> dict:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.infra.clickhouse import ClickHouse
    from app.infra.storage import ObjectStore
    s = get_settings()
    checks: dict[str, dict] = {}
    try:
        with SessionLocal() as db:
            current = db.scalar(text("SELECT version_num FROM alembic_version"))
            beat = db.scalar(text("SELECT max(heartbeat_at) FROM jobs"))
        import pathlib
        cfg = Config(str(pathlib.Path(__file__).resolve().parents[3] / "alembic.ini"))
        cfg.set_main_option("script_location", str(pathlib.Path(__file__).resolve().parents[3] / "alembic"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        checks["postgres"] = {"ok": True}
        checks["migrations"] = {"ok": current == head, "current": current, "head": head}
        checks["worker"] = {"ok": None, "last_heartbeat": beat.isoformat() if beat else None,
                            "note": "null when no generic job has run yet"}
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": type(exc).__name__}
    ch = ClickHouse()
    checks["clickhouse"] = {"ok": ch.ping(), "configured": True} if ch.configured else {"ok": None, "configured": False}
    store = ObjectStore()
    checks["minio"] = {"ok": store.ping(), "configured": True} if store.configured else {"ok": None, "configured": False}
    if s.redis_url:
        try:
            import redis
            checks["redis"] = {"ok": bool(redis.Redis.from_url(s.redis_url, socket_timeout=2).ping()), "configured": True}
        except Exception:
            checks["redis"] = {"ok": False, "configured": True}
    else:
        checks["redis"] = {"ok": None, "configured": False}
    required_ok = checks["postgres"]["ok"] and checks.get("migrations", {}).get("ok")
    optional_ok = all(c["ok"] is not False for k, c in checks.items() if k in ("clickhouse", "minio", "redis"))
    return {"ready": bool(required_ok and optional_ok), "checks": checks}


@router.get("/providers/status")
def providers_status(db: Session = Depends(get_db)):
    from app.providers import registry, runtime
    return {"providers": registry.status(), "budget": runtime.budget_status(db),
            "note": "Global provider spend caps; calls are refused before exceeding them."}


@router.get("/system/observability")
def system_observability(db: Session = Depends(get_db)):
    from app.infra import observability as obs
    obs.sample_gauges()
    return obs.evaluate_slos(db)


@router.get("/system/data-quality")
def system_data_quality(db: Session = Depends(get_db)):
    from app.infra import observability as obs
    from app.infra.clickhouse import ClickHouse
    ch = ClickHouse()
    return obs.check_data_quality_contracts(db, ch if ch.configured else None)
