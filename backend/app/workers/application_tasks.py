"""
Application apply-queue tasks.

When a user adds a job to their apply queue (POST /api/applications/queue/N),
the API creates an Application row in TAILORING status and dispatches
`tailor_application(app_id)` here.  This task:

  1. Loads the user's active master resume (resume_versions where is_active=true)
  2. Loads the job (title + company + description)
  3. Calls ai_writer.tailor_resume() — Claude rewrites the resume bullets to
     surface the most JD-relevant experience (minimal changes, no fabrication)
  4. Calls ai_writer.fit_summary() — Claude computes a 0-100 fit score and
     strengths/gaps for the user to see in the review UI
  5. Writes results back to the Application row and flips status to
     READY_FOR_REVIEW.  If anything fails the row goes to TAILOR_FAILED
     with the error preserved in `tailoring_error` so the user can retry.

The user then opens the queue UI, reviews/edits the tailored resume, and
hits "Submit" — at which point the Application moves to APPLIED (or fires
the auto-apply Playwright pipeline if they want headless submit).
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import structlog
from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


def _run_async(coro):
    async def _wrapper():
        from app.database import async_engine
        await async_engine.dispose()
        return await coro
    return asyncio.run(_wrapper())


async def _tailor_application_async(app_id: int) -> dict:
    from app.database import AsyncSessionLocal
    from app.models.application import Application, ApplicationStatus
    from app.models.resume import ResumeVersion
    from app.models.job import Job
    from app.services.ai_writer import tailor_resume, fit_summary

    async with AsyncSessionLocal() as db:
        # Load application + job (eager-loaded relationships via separate queries)
        app = (await db.execute(
            select(Application).where(Application.id == app_id)
        )).scalar_one_or_none()
        if not app:
            return {"ok": False, "error": f"application {app_id} not found"}

        job = (await db.execute(
            select(Job).where(Job.id == app.job_id)
        )).scalar_one_or_none()
        if not job:
            app.status = ApplicationStatus.TAILOR_FAILED
            app.tailoring_error = f"job {app.job_id} not found"
            await db.commit()
            return {"ok": False, "error": app.tailoring_error}

        # Pick the user's active master resume; fall back to most-recent if none flagged active
        resume = (await db.execute(
            select(ResumeVersion)
            .where(ResumeVersion.user_id == app.user_id, ResumeVersion.is_active == True)
            .limit(1)
        )).scalar_one_or_none()
        if not resume:
            resume = (await db.execute(
                select(ResumeVersion)
                .where(ResumeVersion.user_id == app.user_id)
                .order_by(ResumeVersion.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()

        if not resume or not (resume.content or "").strip():
            app.status = ApplicationStatus.TAILOR_FAILED
            app.tailoring_error = (
                "no resume found for user — upload one at /api/resumes first"
            )
            await db.commit()
            return {"ok": False, "error": app.tailoring_error}

        # Mark as in-progress
        app.status = ApplicationStatus.TAILORING
        await db.commit()

        # ── Tailor ─────────────────────────────────────────────────────────
        try:
            tailored = tailor_resume(
                resume_text=resume.content,
                job_title=job.title or "Software Engineer",
                company_name=job.company_name or "the company",
                job_description=(job.description or "")[:5000],
            )
        except Exception as e:
            logger.exception("tailor_resume_failed", app_id=app_id, err=str(e))
            app.status = ApplicationStatus.TAILOR_FAILED
            app.tailoring_error = f"tailor_resume: {str(e)[:300]}"
            await db.commit()
            return {"ok": False, "error": app.tailoring_error}

        # ── Fit summary (optional; non-blocking on failure) ─────────────────
        fit_data: dict = {}
        fit_score: Optional[int] = None
        try:
            raw = fit_summary(
                resume_text=resume.content,
                job_title=job.title or "",
                job_description=(job.description or "")[:5000],
            )
            # ai_writer returns a JSON string; parse defensively
            if isinstance(raw, dict):
                fit_data = raw
            else:
                # Pull the first {...} block out of the response
                import re
                m = re.search(r"\{[\s\S]*\}", raw or "")
                if m:
                    fit_data = json.loads(m.group(0))
            sc = fit_data.get("fit_score")
            if isinstance(sc, (int, float)):
                fit_score = int(max(0, min(100, sc)))
        except Exception as e:
            logger.debug("fit_summary_failed", app_id=app_id, err=str(e))
            # not fatal — tailored resume is the main deliverable

        # ── Write back + flip to READY_FOR_REVIEW ──────────────────────────
        app.tailored_resume_md = tailored
        app.fit_score = fit_score
        app.fit_summary_json = fit_data or None
        app.status = ApplicationStatus.READY_FOR_REVIEW
        app.tailoring_error = None
        await db.commit()

    logger.info(
        "tailor_application_done",
        app_id=app_id, fit_score=fit_score, resume_len=len(tailored),
    )
    return {"ok": True, "app_id": app_id, "fit_score": fit_score}


@celery_app.task(
    name="app.workers.application_tasks.tailor_application",
    soft_time_limit=120, max_retries=1,
)
def tailor_application(app_id: int) -> dict:
    """Generate the tailored resume + fit summary for one queued application."""
    return _run_async(_tailor_application_async(app_id))
