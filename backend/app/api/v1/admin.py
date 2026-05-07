"""Admin endpoints."""
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, func

from app.database import get_db
from app.core.dependencies import get_current_admin
from app.models.user import User
from app.models.company import Company
from app.models.job import Job
from app.models.ai_models import ScanRun
from app.services.observability import get_observability_summary

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/realtime/status")
async def realtime_monitor_status(
    current_user: User = Depends(get_current_admin),
):
    """Live stats for the real-time job monitor (watermarks, tick count, new jobs)."""
    from app.services.realtime_monitor import get_monitor_stats
    return get_monitor_stats()



@router.get("/system-health")
async def system_health(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    return await get_observability_summary(db)


@router.get("/companies/failing")
async def failing_companies(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    result = await db.execute(
        select(Company).where(
            and_(Company.active == True, Company.consecutive_failures >= 3)
        ).order_by(Company.consecutive_failures.desc()).limit(100)
    )
    companies = result.scalars().all()
    return [
        {
            "id": c.id, "name": c.name, "domain": c.domain,
            "ats_type": c.ats_type, "consecutive_failures": c.consecutive_failures,
            "last_checked_at": c.last_checked_at.isoformat() if c.last_checked_at else None,
        }
        for c in companies
    ]


@router.post("/companies/{company_id}/retry")
async def retry_company(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company.consecutive_failures = 0
    company.next_scan_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": f"Company {company_id} queued for retry"}


@router.post("/companies/{company_id}/disable")
async def disable_company(
    company_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company.active = False
    await db.commit()
    return {"message": f"Company {company_id} disabled"}


@router.post("/reprocess-jobs")
async def reprocess_jobs(
    batch_size: int = Query(100, ge=10, le=500),
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """
    Backfill skill extraction + role classification for existing jobs that
    have empty required_skills or a stale/wrong role_category.

    This is a one-shot repair endpoint — run it after deploying the upgraded
    pipeline to re-enrich all jobs already in the database.

    Query params:
      batch_size — jobs processed per DB commit (default 100)
      dry_run    — if true, compute but do not write to DB
    """
    from app.ai.skill_extractor import extract_skills
    from app.services.normalizer import (
        classify_role_category,
        normalize_country,
        normalize_remote,
        classify_experience_level,
        parse_location,
    )

    # Count jobs needing reprocessing
    needs_skills_q = select(func.count(Job.id)).where(
        Job.active == True,
        (Job.required_skills == None) | (Job.required_skills == [])
    )
    total_to_process = (await db.execute(needs_skills_q)).scalar() or 0

    processed = 0
    updated   = 0
    offset    = 0

    while True:
        result = await db.execute(
            select(Job)
            .where(Job.active == True)
            .order_by(Job.id)
            .limit(batch_size)
            .offset(offset)
        )
        jobs = result.scalars().all()
        if not jobs:
            break

        for job in jobs:
            title       = job.title or ""
            description = job.description or ""
            location    = job.location or ""

            # Skills
            skills = extract_skills(title, description)

            # Role — re-classify using improved classifier
            new_role = classify_role_category(title, description)

            # Location enrichment
            city, region = parse_location(location)
            country      = normalize_country(location)
            remote_type  = normalize_remote(location, title, description)
            exp_level    = classify_experience_level(title, description)

            changed = (
                job.required_skills  != skills.required_skills
                or job.role_category != new_role
                or job.country       != country
            )

            if changed and not dry_run:
                job.required_skills  = skills.required_skills
                job.preferred_skills = skills.preferred_skills
                job.matched_tools    = skills.all_skills
                job.role_category    = new_role
                job.country          = country
                job.remote_type      = remote_type
                job.city             = city
                job.region           = region
                job.experience_level = exp_level

            processed += 1
            if changed:
                updated += 1

        if not dry_run:
            await db.commit()

        offset += batch_size
        # Stop after one full pass (offset now covers all jobs)
        if len(jobs) < batch_size:
            break

    return {
        "dry_run":            dry_run,
        "total_to_process":   total_to_process,
        "jobs_scanned":       processed,
        "jobs_updated":       updated if not dry_run else 0,
        "jobs_would_update":  updated if dry_run else None,
    }


@router.get("/scans/{scan_id}/raw-response")
async def get_scan_raw(
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    result = await db.execute(select(ScanRun).where(ScanRun.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    return {
        "id": scan.id, "status": scan.status,
        "jobs_fetched": scan.jobs_fetched, "jobs_new": scan.jobs_new,
        "raw_response_path": scan.raw_response_path,
        "error_message": scan.error_message,
        "started_at": scan.started_at.isoformat(),
    }


# ── Company Discovery ──────────────────────────────────────────────────────────

@router.post("/discovery/run")
async def run_company_discovery(
    max_companies: int = Query(10_000, ge=100, le=30_000),
    current_user: User = Depends(get_current_admin),
):
    """
    Seed the company registry from public ATS boards, YC dataset, and
    the curated high-signal list.

    Runs in the background — returns immediately with a confirmation.
    Check logs for 'company_discovery_complete' to see results.

    Query params:
      max_companies — upper bound for this discovery run (default 10 000)
    """
    import asyncio
    from app.services.company_discovery import discover_companies

    async def _run():
        try:
            metrics = await discover_companies(max_companies=max_companies)
            import logging
            logging.getLogger(__name__).info(
                "admin_discovery_complete metrics=%s", metrics
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("admin_discovery_error error=%s", exc)

    asyncio.create_task(_run())
    return {
        "status":        "started",
        "max_companies": max_companies,
        "message":       "Company discovery running in background. Watch server logs.",
    }


@router.post("/expansion/run")
async def run_company_expansion(
    max_new: int = Query(20_000, ge=1_000, le=50_000),
    current_user: User = Depends(get_current_admin),
):
    """
    Expand the company registry beyond the initial seed using:
      - Domain permutations (TLD + suffix variants)
      - GitHub org lookup
      - Career page detection

    Runs in the background. Watch server logs for 'company_expander_complete'.
    """
    import asyncio
    from app.services.company_expander import run_expansion

    async def _run():
        try:
            metrics = await run_expansion(max_new=max_new)
            import logging
            logging.getLogger(__name__).info(
                "admin_expansion_complete metrics=%s", metrics
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("admin_expansion_error error=%s", exc)

    asyncio.create_task(_run())
    return {
        "status":   "started",
        "max_new":  max_new,
        "message":  "Company expansion running in background. Watch server logs.",
    }


@router.post("/validation/run")
async def run_company_validation(
    limit: int = Query(200, ge=10, le=2_000),
    concurrency: int = Query(10, ge=1, le=30),
    current_user: User = Depends(get_current_admin),
):
    """
    Validate up to `limit` companies (prioritising those never validated or
    with the most failures). Updates quality scores and deactivates companies
    that fail twice.

    Runs in the background — returns immediately.
    """
    import asyncio
    from app.services.company_validator import validate_and_update_companies

    async def _run():
        try:
            metrics = await validate_and_update_companies(
                limit=limit, concurrency=concurrency
            )
            import logging
            logging.getLogger(__name__).info(
                "admin_validation_complete metrics=%s", metrics
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("admin_validation_error error=%s", exc)

    asyncio.create_task(_run())
    return {
        "status":      "started",
        "limit":       limit,
        "concurrency": concurrency,
        "message":     "Validation pass running in background. Watch server logs.",
    }


@router.post("/cleanup/run")
async def run_stale_cleanup(
    current_user: User = Depends(get_current_admin),
):
    """
    Deactivate companies with no active jobs for the past 7 days.
    Runs in the background.
    """
    import asyncio
    from app.services.company_validator import cleanup_stale_companies

    async def _run():
        try:
            count = await cleanup_stale_companies()
            import logging
            logging.getLogger(__name__).info(
                "admin_cleanup_complete deactivated=%d", count
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("admin_cleanup_error error=%s", exc)

    asyncio.create_task(_run())
    return {"status": "started", "message": "Stale-company cleanup running in background."}


@router.get("/pipeline/metrics")
async def pipeline_metrics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """Live snapshot of company registry and job corpus health."""
    total_companies   = (await db.execute(select(func.count(Company.id)))).scalar() or 0
    active_companies  = (await db.execute(
        select(func.count(Company.id)).where(Company.active == True)
    )).scalar() or 0
    dead_companies    = total_companies - active_companies

    total_jobs        = (await db.execute(select(func.count(Job.id)))).scalar() or 0
    active_jobs       = (await db.execute(
        select(func.count(Job.id)).where(Job.active == True)
    )).scalar() or 0

    failing = (await db.execute(
        select(func.count(Company.id)).where(
            Company.active == True, Company.consecutive_failures >= 1
        )
    )).scalar() or 0

    due_now = (await db.execute(
        select(func.count(Company.id)).where(
            Company.active == True,
            (Company.next_scan_at == None) | (Company.next_scan_at <= datetime.now(timezone.utc))  # noqa: E711
        )
    )).scalar() or 0

    return {
        "companies": {
            "total":    total_companies,
            "active":   active_companies,
            "inactive": dead_companies,
            "failing":  failing,
            "due_for_scan": due_now,
        },
        "jobs": {
            "total":  total_jobs,
            "active": active_jobs,
        },
    }

