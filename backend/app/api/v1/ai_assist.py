"""
AI-assistance endpoints: cover letter, resume tailoring, fit summary,
auto-apply trigger.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.job import Job
from app.models.resume import ResumeVersion
from app.services.ai_writer import (
    generate_cover_letter, tailor_resume, fit_summary,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["ai-assist"])


# ── Shared resume fetcher ────────────────────────────────────────────────────

async def _get_active_resume(db: AsyncSession, user_id: int) -> ResumeVersion:
    result = await db.execute(
        select(ResumeVersion)
        .where(ResumeVersion.user_id == user_id, ResumeVersion.is_active == True)
        .order_by(desc(ResumeVersion.updated_at))
        .limit(1)
    )
    rv = result.scalar_one_or_none()
    if not rv:
        raise HTTPException(status_code=400, detail="No active resume — upload one first")
    return rv


async def _get_job(db: AsyncSession, job_id: int) -> Job:
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── Cover letter ─────────────────────────────────────────────────────────────

class CoverLetterResponse(BaseModel):
    job_id: int
    cover_letter: str
    provider: str


@router.post("/cover_letter/{job_id}", response_model=CoverLetterResponse)
async def cover_letter(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import os
    rv = await _get_active_resume(db, current_user.id)
    job = await _get_job(db, job_id)

    text = generate_cover_letter(
        resume_text=rv.content or "",
        job_title=job.title,
        company_name=job.company_name,
        job_description=job.description or "",
        user_full_name=current_user.full_name,
    )
    provider = (os.environ.get("AI_PROVIDER")
                or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY")
                    else "openai" if os.environ.get("OPENAI_API_KEY")
                    else "template"))
    return CoverLetterResponse(job_id=job_id, cover_letter=text, provider=provider)


# ── Resume tailoring ─────────────────────────────────────────────────────────

class TailorResponse(BaseModel):
    job_id: int
    tailored_resume: str
    provider: str


@router.post("/tailor_resume/{job_id}", response_model=TailorResponse)
async def tailor_resume_endpoint(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import os
    rv = await _get_active_resume(db, current_user.id)
    job = await _get_job(db, job_id)

    text = tailor_resume(
        resume_text=rv.content or "",
        job_title=job.title,
        company_name=job.company_name,
        job_description=job.description or "",
    )
    provider = (os.environ.get("AI_PROVIDER")
                or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY")
                    else "openai" if os.environ.get("OPENAI_API_KEY")
                    else "template"))
    return TailorResponse(job_id=job_id, tailored_resume=text, provider=provider)


# ── Fit summary (small) ──────────────────────────────────────────────────────

class FitResponse(BaseModel):
    job_id: int
    fit_summary_json: str


@router.post("/fit/{job_id}", response_model=FitResponse)
async def fit_endpoint(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rv = await _get_active_resume(db, current_user.id)
    job = await _get_job(db, job_id)
    return FitResponse(
        job_id=job_id,
        fit_summary_json=fit_summary(
            resume_text=rv.content or "",
            job_title=job.title,
            job_description=job.description or "",
        ),
    )


# ── Auto-apply trigger (handled in auto_apply.py service) ───────────────────

class AutoApplyRequest(BaseModel):
    job_ids: list[int]
    dry_run: bool = True       # default: fill but don't submit


class AutoApplyResponse(BaseModel):
    queued: int
    results: list[dict]


@router.post("/auto_apply", response_model=AutoApplyResponse)
async def auto_apply_endpoint(
    req: AutoApplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Queue auto-apply for the given job IDs via Celery — the worker container
    is where Playwright + Chromium are installed.

    Real submission only happens if dry_run=false.  When dry_run=true (default)
    the bot fills the form, captures a screenshot, and reports back.
    """
    from app.workers.auto_apply_tasks import run_one
    rv = await _get_active_resume(db, current_user.id)
    # Build a profile dict from User model — the worker forwards it to Playwright
    profile = {
        "full_name":    current_user.full_name or "",
        "email":        current_user.email,
        "linkedin":     current_user.linkedin_url or "",
        "github":       current_user.github_url or "",
        "portfolio":    current_user.portfolio_url or "",
        "location":     current_user.current_location or "",
        "work_authorization": current_user.work_authorization or "Yes",
    }
    results = []
    for jid in req.job_ids[:20]:
        job = await _get_job(db, jid)
        run_one.delay(
            user_id=current_user.id,
            user_email=current_user.email,
            user_name=current_user.full_name or "Applicant",
            resume_path=rv.file_path or "",
            resume_text=rv.content or "",
            job_id=jid,
            job_url=job.apply_url or job.url or "",
            company=job.company_name,
            title=job.title,
            dry_run=req.dry_run,
            profile=profile,
        )
        results.append({"job_id": jid, "status": "queued",
                        "company": job.company_name, "title": job.title})
    return AutoApplyResponse(queued=len(results), results=results)
