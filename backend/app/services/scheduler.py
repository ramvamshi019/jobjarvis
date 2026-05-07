"""
Scheduler — 4 cadences for the public job aggregation platform.

  Cadence 1 — Job fetch      : every 5 minutes  (active companies)
  Cadence 2 — Company discovery : every 6 hours
  Cadence 3 — Cleanup        : daily at 03:00 UTC
  Cadence 4 — Realtime monitor : every 60 s (embedded in FastAPI via asyncio task)

EVENT LOOP ARCHITECTURE
──────────────────────
Single asyncio.run() in __main__ → one event loop for the entire process.
AsyncIOScheduler shares that loop.  No nested asyncio.run() anywhere.
"""
from __future__ import annotations

import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.job_pipeline import run_ingestion_pipeline, run_priority_scan
from app.services.company_discovery import run_company_discovery
from app.services.cleanup import run_cleanup

logger = logging.getLogger(__name__)


# ── Wrapper coroutines ─────────────────────────────────────────────────────────

async def _job_fetch() -> None:
    """
    Micro-batch priority scan — fires every 60 seconds.

    Each tick picks the top 20 companies by scan_priority whose next_scan_at
    is due, processes them concurrently, and exits.  Hot companies (new jobs
    found) get a +0.2 boost so they surface again quickly; dead companies
    decay *0.8 each miss and fall out of the top-20 naturally.

    The full run_ingestion_pipeline() is still available for manual triggers
    or FORCE_SCAN=1 dev mode.
    """
    logger.info("[SCHEDULER] priority_scan start")
    t = time.monotonic()
    try:
        await run_priority_scan()
    except Exception as exc:
        logger.error("[SCHEDULER] priority_scan error=%s", exc)
    finally:
        logger.info("[SCHEDULER] priority_scan done elapsed_s=%.1f", time.monotonic() - t)


async def _company_discovery() -> None:
    logger.info("[SCHEDULER] discovery start")
    t = time.monotonic()
    try:
        result = await run_company_discovery()
        logger.info("[SCHEDULER] discovery done %s elapsed_s=%.1f", result, time.monotonic() - t)
    except Exception as exc:
        logger.error("[SCHEDULER] discovery error=%s", exc)


async def _cleanup() -> None:
    logger.info("[SCHEDULER] cleanup start")
    t = time.monotonic()
    try:
        result = await run_cleanup()
        logger.info("[SCHEDULER] cleanup done %s elapsed_s=%.1f", result, time.monotonic() - t)
    except Exception as exc:
        logger.error("[SCHEDULER] cleanup error=%s", exc)


# ── Build scheduler ────────────────────────────────────────────────────────────

def build_async_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Cadence 1: Priority micro-batch scan every 60 seconds.
    # Each tick processes the top-20 due companies ordered by scan_priority.
    # max_instances=1 + coalesce=True ensure no overlapping runs even if a
    # batch takes longer than 60 s (e.g. slow ATS endpoints).
    scheduler.add_job(
        _job_fetch,
        "interval",
        seconds=60,
        id="job_fetch",
        name="Priority micro-batch scan (every 60 s)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    # Cadence 2: Company discovery every 6 hours
    scheduler.add_job(
        _company_discovery,
        "interval",
        hours=6,
        id="company_discovery",
        name="Company discovery (every 6 h)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # Cadence 3: Cleanup daily at 03:00 UTC
    scheduler.add_job(
        _cleanup,
        "cron",
        hour=3,
        minute=0,
        id="cleanup",
        name="Daily cleanup (03:00 UTC)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )

    return scheduler


# ── Standalone entry point ─────────────────────────────────────────────────────

async def start_scheduler_async() -> None:
    """
    Standalone scheduler process.
    One asyncio.run() → one event loop → all DB sessions share the same loop.
    """
    from app.config import settings
    from sqlalchemy import select, func
    from app.database import AsyncSessionLocal
    from app.models.company import Company

    db_url_clean = settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else settings.DATABASE_URL
    logger.info("[SCHEDULER] init db=%s", db_url_clean)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count(Company.id)).where(Company.active == True)
        )
        count = result.scalar() or 0

    logger.info("[SCHEDULER] active_companies=%d", count)
    if count == 0:
        logger.warning("[SCHEDULER] no companies — run discovery first")

    # Build and start scheduler
    scheduler = build_async_scheduler()
    scheduler.start()

    # Run pipeline immediately on startup
    logger.info("[SCHEDULER] running initial pipeline")
    await _job_fetch()

    # Run discovery immediately if no companies
    if count == 0:
        logger.info("[SCHEDULER] running initial discovery")
        await _company_discovery()

    logger.info("[SCHEDULER] ready — priority_scan=60s discovery=6h cleanup=daily")

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
        logger.info("[SCHEDULER] stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    asyncio.run(start_scheduler_async())
