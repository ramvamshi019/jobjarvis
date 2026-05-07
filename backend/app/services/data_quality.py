"""Data quality engine: validates jobs and produces quality reports."""
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.ai_models import DataQualityReport

logger = structlog.get_logger(__name__)


class DataQualityCheck:
    name: str
    description: str
    severity: str  # error|warning|info

    def __init__(self, name: str, description: str, severity: str = "warning"):
        self.name = name
        self.description = description
        self.severity = severity


CHECKS = [
    DataQualityCheck("missing_title", "Job has no title", "error"),
    DataQualityCheck("missing_company", "Job has no company name", "error"),
    DataQualityCheck("missing_url", "Job has no URL", "warning"),
    DataQualityCheck("empty_description", "Job description is empty", "warning"),
    DataQualityCheck("stale_job", "Job not seen in 14+ days", "info"),
    DataQualityCheck("duplicate_fingerprint", "Possible duplicate fingerprint", "warning"),
    DataQualityCheck("invalid_salary", "Salary min > salary max", "warning"),
]


async def run_quality_report(db: AsyncSession) -> DataQualityReport:
    """Run all checks and persist a quality report."""
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(days=14)

    # Count total active jobs
    total_q = await db.execute(select(func.count(Job.id)).where(Job.active == True))
    total = total_q.scalar() or 0

    missing_title_q = await db.execute(
        select(func.count(Job.id)).where(and_(Job.active == True, (Job.title == None) | (Job.title == '')))
    )
    missing_title = missing_title_q.scalar() or 0

    missing_company_q = await db.execute(
        select(func.count(Job.id)).where(and_(Job.active == True, (Job.company_name == None) | (Job.company_name == '')))
    )
    missing_company = missing_company_q.scalar() or 0

    missing_url_q = await db.execute(
        select(func.count(Job.id)).where(and_(Job.active == True, (Job.job_url == None) | (Job.job_url == '')))
    )
    missing_url = missing_url_q.scalar() or 0

    empty_desc_q = await db.execute(
        select(func.count(Job.id)).where(and_(Job.active == True, (Job.description == None) | (Job.description == '')))
    )
    empty_description = empty_desc_q.scalar() or 0

    stale_q = await db.execute(
        select(func.count(Job.id)).where(and_(Job.active == True, Job.last_seen_at < stale_threshold))
    )
    stale_jobs = stale_q.scalar() or 0

    report = DataQualityReport(
        total_jobs=total,
        missing_title=missing_title,
        missing_company=missing_company,
        missing_url=missing_url,
        empty_description=empty_description,
        stale_jobs=stale_jobs,
        report_json={
            "quality_score": round((1 - (missing_title + missing_company) / max(total, 1)) * 100, 1),
            "checked_at": now.isoformat(),
        }
    )
    db.add(report)
    await db.commit()
    logger.info("quality_report", total=total, missing_title=missing_title, stale=stale_jobs)
    return report
