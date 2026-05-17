"""Dedup engine: three-level deduplication strategy.

compute_fingerprint is defined in normalizer.py (pure utility, no app deps)
and re-exported here for backward compatibility with any existing callers that
import it from dedup.
"""
from typing import Optional
import structlog
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.core.security import hash_content
# Step 2: fingerprint now lives in normalizer.py — re-export for backward compat
from app.services.normalizer import compute_fingerprint  # noqa: F401 (re-export)

logger = structlog.get_logger(__name__)


class DedupEngine:
    """
    Three-level dedup:
    1. company_id + external_id  (strongest — same ATS posting)
    2. job_url                   (same URL = same job)
    3. fingerprint               (title + company + city)
    """

    async def find_duplicate(
        self,
        db: AsyncSession,
        company_id: int,
        external_id: Optional[str],
        job_url: Optional[str],
        fingerprint: str,
    ) -> Optional[Job]:
        """Return existing Job if duplicate found, else None."""

        conditions = [Job.fingerprint == fingerprint]

        if external_id:
            conditions.insert(0, (Job.company_id == company_id) & (Job.external_id == external_id))

        if job_url:
            conditions.insert(1, Job.job_url == job_url)

        result = await db.execute(
            select(Job).where(or_(*conditions)).limit(1)
        )
        return result.scalar_one_or_none()

    async def is_duplicate(
        self,
        db: AsyncSession,
        company_id: int,
        external_id: Optional[str],
        job_url: Optional[str],
        fingerprint: str,
    ) -> bool:
        existing = await self.find_duplicate(db, company_id, external_id, job_url, fingerprint)
        return existing is not None

    async def upsert_job(
        self,
        db: AsyncSession,
        job_data: dict,
    ) -> tuple[Job, bool]:
        """
        Insert new job or update existing.
        Returns (job, is_new).
        """
        fp = job_data.get("fingerprint")
        ext_id = job_data.get("external_id")
        company_id = job_data.get("company_id")
        job_url = job_data.get("job_url")

        existing = await self.find_duplicate(db, company_id, ext_id, job_url, fp)

        if existing:
            # Update last_seen_at and check for changes
            from datetime import datetime, timezone
            existing.last_seen_at = datetime.now(timezone.utc)
            if job_data.get("active") is not None:
                existing.active = job_data["active"]
            # Update raw_hash to detect description changes
            if job_data.get("raw_hash") and job_data["raw_hash"] != existing.raw_hash:
                existing.raw_hash = job_data["raw_hash"]
            # Self-heal freshness: if the source now gives us a real posting
            # date that we didn't have before (e.g. the Workday connector
            # learned to parse it), backfill it and recompute a stable,
            # posting-date-based label. We only touch rows that lacked a
            # posted_at so we never churn a known date or relabel a
            # date-less job as perpetually "new".
            incoming_posted = job_data.get("posted_at")
            if incoming_posted and not existing.posted_at:
                from app.services.freshness import compute_freshness
                existing.posted_at = incoming_posted
                existing.freshness_label = compute_freshness(incoming_posted)
            await db.flush()
            return existing, False

        # New job
        new_job = Job(**{k: v for k, v in job_data.items() if hasattr(Job, k)})
        db.add(new_job)
        await db.flush()
        logger.debug("new_job", title=job_data.get("title"), company_id=company_id)
        return new_job, True
