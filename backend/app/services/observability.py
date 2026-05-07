"""System observability — metrics, health, and monitoring."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_models import ScanRun, AIUsageLog, FetchAuditLog
from app.models.job import Job
from app.models.company import Company
from app.models.ai_models import AIDecision


async def get_observability_summary(db: AsyncSession) -> dict:
    """Collect system-wide metrics for the observability dashboard."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    day_ago  = now - timedelta(days=1)

    # Scans in last hour
    scans_per_hour = (await db.execute(
        select(func.count(ScanRun.id)).where(ScanRun.started_at >= hour_ago)
    )).scalar() or 0

    # Jobs found today
    jobs_found_today = (await db.execute(
        select(func.count(Job.id)).where(Job.first_seen_at >= day_ago)
    )).scalar() or 0

    # Active jobs total
    active_jobs_total = (await db.execute(
        select(func.count(Job.id)).where(Job.active == True)
    )).scalar() or 0

    # Failing companies (≥3 consecutive failures)
    failing_companies = (await db.execute(
        select(func.count(Company.id)).where(
            and_(Company.active == True, Company.consecutive_failures >= 3)
        )
    )).scalar() or 0

    # Total active companies
    total_companies = (await db.execute(
        select(func.count(Company.id)).where(Company.active == True)
    )).scalar() or 0

    # Recent scan stats
    scan_row = (await db.execute(
        select(
            func.avg(ScanRun.duration_seconds).label("avg_duration"),
            func.sum(ScanRun.jobs_new).label("total_new"),
        ).where(ScanRun.started_at >= day_ago)
    )).one()

    # AI usage today
    ai_row = (await db.execute(
        select(
            func.sum(AIUsageLog.estimated_cost).label("total_cost"),
            func.count(AIUsageLog.id).label("total_calls"),
        ).where(AIUsageLog.created_at >= day_ago)
    )).one()

    # Fetch API success rate (last hour) — use two simple COUNT queries instead
    # of func.sum(func.cast(...)) which has SQLAlchemy type-inference quirks.
    fetch_total = (await db.execute(
        select(func.count(FetchAuditLog.id))
        .where(FetchAuditLog.fetched_at >= hour_ago)
    )).scalar() or 0

    fetch_success = (await db.execute(
        select(func.count(FetchAuditLog.id))
        .where(and_(FetchAuditLog.fetched_at >= hour_ago, FetchAuditLog.success == True))
    )).scalar() or 0

    api_success_rate = round(fetch_success / max(fetch_total, 1) * 100, 1)

    return {
        "scans_per_hour":            scans_per_hour,
        "jobs_found_today":          jobs_found_today,
        "active_jobs_total":         active_jobs_total,
        "failing_companies":         failing_companies,
        "total_companies":           total_companies,
        "avg_scan_duration_seconds": round(float(scan_row.avg_duration or 0), 2),
        "jobs_inserted_today":       int(scan_row.total_new or 0),
        "ai_cost_today_usd":         round(float(ai_row.total_cost or 0), 4),
        "ai_calls_today":            int(ai_row.total_calls or 0),
        "api_success_rate_pct":      api_success_rate,
        "fetch_requests_last_hour":  fetch_total,
        "system_healthy":            failing_companies < max(total_companies * 0.1, 1),
        "measured_at":               now.isoformat(),
    }
