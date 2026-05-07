"""Health check endpoint — public, no auth required."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, func, select
from app.database import get_db
from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Liveness + readiness probe.
    Returns 200 whether healthy or degraded — never raises an exception.
    Check the 'status' field to determine actual health.
    """
    db_status = "ok"
    db_type = "postgresql"
    jobs_count = 0

    # DB ping
    try:
        await db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {str(exc)[:100]}"

    # Count active jobs (informational — never crash)
    try:
        from app.models.job import Job
        q = await db.execute(select(func.count(Job.id)).where(Job.active == True))
        jobs_count = q.scalar() or 0
    except Exception:
        jobs_count = -1  # Table may not be ready on very first startup

    # AI key availability
    ai_status = {
        "openai": "configured" if settings.OPENAI_API_KEY else "no_key",
        "anthropic": "configured" if settings.ANTHROPIC_API_KEY else "no_key",
        "mode": "live" if (settings.OPENAI_API_KEY or settings.ANTHROPIC_API_KEY) else "mock",
    }

    overall = "ok" if db_status == "ok" else "degraded"

    return {
        "status": overall,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "db": {
            "status": db_status,
            "type": db_type,
            "active_jobs": jobs_count,
        },
        "ai": ai_status,
    }
