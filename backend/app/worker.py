"""Background worker: claims crawl and sync jobs from Postgres (FOR UPDATE SKIP LOCKED).

Run several instances safely: a job is only ever claimed by one worker.
"""

import asyncio
import logging
import os
import signal
import socket

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app import jobs
from app.services import automation, backlinks, rank_tracking  # noqa: F401  (registers job handlers)
from app.services import crawl_service, google_service

log = logging.getLogger("app.worker")


async def run_once(worker_id: str) -> bool:
    """Process at most one job. Returns True if a job was processed."""
    with SessionLocal() as db:
        crawl_service.recover_stale_crawls(db)
        run = crawl_service.claim_next_crawl(db, worker_id)
        crawl_id = run.id if run else None
    if crawl_id:
        log.info("crawl %s started", crawl_id)
        await crawl_service.execute_crawl(crawl_id)
        return True
    with SessionLocal() as db:
        job = google_service.claim_next_sync(db, worker_id)
        if job:
            log.info("sync job %s (%s) started", job.id, job.source)
            await google_service.run_sync_job(db, job)
            return True
    with SessionLocal() as db:
        jobs.scheduler_tick(db)
        job = jobs.claim(db, worker_id)
        job_id = job.id if job else None
    if job_id:
        await jobs.run(job_id)
        return True
    return False


async def main() -> None:
    setup_logging()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker %s ready", worker_id)
    while not stop.is_set():
        try:
            busy = await run_once(worker_id)
        except Exception:
            log.exception("worker loop error")
            busy = False
        if not busy:
            try:
                await asyncio.wait_for(stop.wait(), timeout=get_settings().worker_poll_interval_s)
            except TimeoutError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
