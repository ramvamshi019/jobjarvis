"""
Daily cleanup job.
- Deactivate jobs not seen in > 7 days
- Purge jobs not seen in > 30 days
- Reset next_scan_at for stale companies
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, select, update

from app.database import AsyncSessionLocal
from app.models.company import Company
from app.models.job import Job

logger = logging.getLogger(__name__)


async def run_cleanup() -> dict:
    logger.info("[CLEANUP] start")
    t0  = time.monotonic()
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        # 1. Deactivate jobs not seen in 7 days
        stale_cutoff = now - timedelta(days=7)
        deactivated = await db.execute(
            update(Job)
            .where(and_(Job.active == True, Job.last_seen_at < stale_cutoff))
            .values(active=False)
        )

        # 2. Hard-delete jobs older than 30 days (not seen)
        purge_cutoff = now - timedelta(days=30)
        purged = await db.execute(
            delete(Job).where(
                and_(Job.active == False, Job.last_seen_at < purge_cutoff)
            )
        )

        # 3. Reset next_scan_at for companies that are stuck
        stuck_cutoff = now - timedelta(hours=12)
        await db.execute(
            update(Company)
            .where(
                and_(
                    Company.active == True,
                    Company.next_scan_at < stuck_cutoff,
                )
            )
            .values(next_scan_at=now)
        )

        await db.commit()

    elapsed = round(time.monotonic() - t0, 1)
    result  = {
        "deactivated_jobs": deactivated.rowcount,
        "purged_jobs":      purged.rowcount,
        "runtime_s":        elapsed,
    }
    logger.info("[CLEANUP] done %s", result)
    return result
