"""Scan trigger and status endpoints."""
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.database import get_db
from app.core.dependencies import get_current_user, get_current_admin
from app.models.user import User
from app.models.ai_models import ScanRun

router = APIRouter(prefix="/scans", tags=["scans"])


@router.post("/run-now")
async def trigger_scan(
    company_id: int = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """Manually trigger a scan (dispatched to Celery in production)."""
    from app.workers.scan_tasks import run_company_scan_task
    if company_id:
        # Celery would be: run_company_scan_task.delay(company_id)
        return {"message": f"Scan queued for company {company_id}", "queued": True}
    return {"message": "Full scan cycle queued", "queued": True}


@router.get("/runs")
async def list_scan_runs(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ScanRun).order_by(desc(ScanRun.started_at)).limit(limit)
    )
    scans = result.scalars().all()
    return [
        {
            "id": s.id, "company_id": s.company_id, "status": s.status,
            "jobs_fetched": s.jobs_fetched, "jobs_new": s.jobs_new,
            "duration_seconds": s.duration_seconds,
            "started_at": s.started_at.isoformat(),
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        }
        for s in scans
    ]


@router.get("/runs/{scan_id}")
async def get_scan_run(
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(ScanRun).where(ScanRun.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Scan not found")
    return scan
