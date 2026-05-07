"""Market intelligence: trending skills, top companies, salary trends."""
from datetime import datetime, timedelta, timezone
from typing import Optional
import structlog
from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.company import Company
from app.models.ai_models import CompanyIntelligence

logger = structlog.get_logger(__name__)


async def get_market_trends(db: AsyncSession, days: int = 30) -> dict:
    """Compute market intelligence from recent job data."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Top hiring companies
    top_companies_q = await db.execute(
        select(Job.company_name, func.count(Job.id).label("job_count"))
        .where(and_(Job.active == True, Job.first_seen_at >= cutoff))
        .group_by(Job.company_name)
        .order_by(desc("job_count"))
        .limit(20)
    )
    top_companies = [{"company": row[0], "job_count": row[1]} for row in top_companies_q.fetchall()]

    # Top roles
    top_roles_q = await db.execute(
        select(Job.role_category, func.count(Job.id).label("count"))
        .where(and_(Job.active == True, Job.first_seen_at >= cutoff, Job.role_category != None))
        .group_by(Job.role_category)
        .order_by(desc("count"))
    )
    top_roles = [{"role": row[0], "count": row[1]} for row in top_roles_q.fetchall()]

    # Salary ranges by role
    salary_q = await db.execute(
        select(
            Job.role_category,
            func.avg(Job.salary_min).label("avg_min"),
            func.avg(Job.salary_max).label("avg_max"),
            func.count(Job.id).label("sample_size"),
        )
        .where(and_(
            Job.active == True,
            Job.salary_min != None,
            Job.salary_max != None,
            Job.first_seen_at >= cutoff,
        ))
        .group_by(Job.role_category)
    )
    salary_ranges = [
        {
            "role": row[0],
            "avg_min_salary": int(row[1] or 0),
            "avg_max_salary": int(row[2] or 0),
            "sample_size": row[3],
        }
        for row in salary_q.fetchall()
    ]

    # Remote trends
    remote_q = await db.execute(
        select(Job.remote_type, func.count(Job.id).label("count"))
        .where(and_(Job.active == True, Job.first_seen_at >= cutoff))
        .group_by(Job.remote_type)
    )
    remote_trends = {row[0]: row[1] for row in remote_q.fetchall() if row[0]}

    total_remote = remote_trends.get("remote", 0)
    total_all = sum(remote_trends.values())
    remote_pct = round(total_remote / max(total_all, 1) * 100, 1)

    return {
        "period_days": days,
        "top_hiring_companies": top_companies,
        "top_roles": top_roles,
        "salary_ranges_by_role": salary_ranges,
        "remote_trends": remote_trends,
        "remote_percentage": remote_pct,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
