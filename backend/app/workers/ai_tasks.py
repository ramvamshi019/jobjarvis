"""Celery AI tasks — CareerAgent, data quality, market intelligence."""
import asyncio
import structlog
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


def _run_async(coro):
    """Run async coroutine from a synchronous Celery task.

    asyncio.run() creates a fresh event loop per call, which is the correct
    pattern for sync Celery workers bridging into async code. See scan_tasks.py
    for the full rationale.
    """
    return asyncio.run(coro)


@celery_app.task(name="app.workers.ai_tasks.run_career_agent_all_users", soft_time_limit=1800)
def run_career_agent_all_users():
    return _run_async(_run_agent_all())


@celery_app.task(name="app.workers.ai_tasks.run_career_agent_for_user", soft_time_limit=300)
def run_career_agent_for_user(user_id: int):
    return _run_async(_run_agent_for_user(user_id))


@celery_app.task(name="app.workers.ai_tasks.run_data_quality")
def run_data_quality():
    return _run_async(_run_quality())


@celery_app.task(name="app.workers.ai_tasks.update_company_intelligence")
def update_company_intelligence():
    return _run_async(_update_intelligence())


@celery_app.task(name="app.workers.ai_tasks.run_self_correction_all_users")
def run_self_correction_all_users():
    return _run_async(_run_corrections())


async def _run_agent_all() -> dict:
    from app.database import AsyncSessionLocal
    from app.models.user import User
    from app.ai.agent.career_agent import CareerAgent
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = list(result.scalars().all())

    total = 0
    for user in users:
        run_career_agent_for_user.delay(user.id)
        total += 1
    return {"dispatched": total}


async def _run_agent_for_user(user_id: int) -> dict:
    from app.database import AsyncSessionLocal
    from app.ai.agent.career_agent import CareerAgent

    async with AsyncSessionLocal() as db:
        agent = CareerAgent(db, user_id)
        return await agent.run()


async def _run_quality() -> dict:
    from app.database import AsyncSessionLocal
    from app.services.data_quality import run_quality_report

    async with AsyncSessionLocal() as db:
        report = await run_quality_report(db)
        return {"report_id": report.id, "quality_score": report.report_json.get("quality_score")}


async def _update_intelligence() -> dict:
    from app.database import AsyncSessionLocal
    from app.models.company import Company
    from app.models.ai_models import CompanyIntelligence
    from app.models.job import Job
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select, func, and_

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Company).where(Company.active == True).limit(1000))
        companies = list(result.scalars().all())

        updated = 0
        for company in companies:
            jobs_7d_q = await db.execute(
                select(func.count(Job.id)).where(
                    and_(Job.company_id == company.id, Job.first_seen_at >= week_ago)
                )
            )
            jobs_30d_q = await db.execute(
                select(func.count(Job.id)).where(
                    and_(Job.company_id == company.id, Job.first_seen_at >= month_ago)
                )
            )

            jobs_7d = jobs_7d_q.scalar() or 0
            jobs_30d = jobs_30d_q.scalar() or 0

            intel_q = await db.execute(
                select(CompanyIntelligence).where(CompanyIntelligence.company_id == company.id)
            )
            intel = intel_q.scalar_one_or_none()
            if not intel:
                intel = CompanyIntelligence(company_id=company.id)
                db.add(intel)

            intel.jobs_last_7_days = jobs_7d
            intel.jobs_last_30_days = jobs_30d
            intel.hiring_velocity = jobs_7d / 7.0 if jobs_7d else 0.0
            updated += 1

        await db.commit()
        return {"companies_updated": updated}


async def _run_corrections() -> dict:
    from app.database import AsyncSessionLocal
    from app.models.user import User
    from app.ai.agent.self_corrector import SelfCorrector
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.is_active == True))
        users = list(result.scalars().all())

    total_corrections = 0
    for user in users:
        async with AsyncSessionLocal() as db:
            corrector = SelfCorrector(db, user.id)
            corrections = await corrector.run_corrections()
            total_corrections += len(corrections)

    return {"users_processed": len(users), "total_corrections": total_corrections}
