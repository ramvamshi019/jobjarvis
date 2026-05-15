"""Application tracker + apply-queue endpoints."""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.job import Job
from app.models.application import Application, ApplicationAnswer, ApplicationStatus
from app.schemas.application import ApplicationIn, ApplicationOut, ApplicationUpdate

router = APIRouter(prefix="/applications", tags=["applications"])

# ── Apply queue (Jobright-style review-before-submit flow) ───────────────────
# Status lifecycle:
#   QUEUED → TAILORING → READY_FOR_REVIEW → APPLIED
#                    \→ TAILOR_FAILED  (user can retry)

_QUEUE_STATUSES = (
    ApplicationStatus.QUEUED.value,
    ApplicationStatus.TAILORING.value,
    ApplicationStatus.READY_FOR_REVIEW.value,
    ApplicationStatus.TAILOR_FAILED.value,
)


@router.get("", response_model=list[ApplicationOut])
async def list_applications(
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(Application).where(Application.user_id == current_user.id)
    if status:
        q = q.where(Application.status == status)
    q = q.order_by(desc(Application.created_at)).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.post("", response_model=ApplicationOut, status_code=201)
async def create_application(
    body: ApplicationIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    app = Application(user_id=current_user.id, **body.model_dump())
    if body.status == "applied":
        app.applied_at = datetime.now(timezone.utc)
    db.add(app)
    await db.commit()
    await db.refresh(app)
    return app


@router.patch("/{app_id}", response_model=ApplicationOut)
async def update_application(
    app_id: int,
    body: ApplicationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Application).where(and_(Application.id == app_id, Application.user_id == current_user.id))
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(app, k, v)
    if body.status == "applied" and not app.applied_at:
        app.applied_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(app)
    return app


@router.get("/answers")
async def list_answers(
    question_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(ApplicationAnswer).where(ApplicationAnswer.user_id == current_user.id)
    if question_type:
        q = q.where(ApplicationAnswer.question_type == question_type)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.post("/answers", status_code=201)
async def create_answer(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    answer = ApplicationAnswer(user_id=current_user.id, **body)
    db.add(answer)
    await db.commit()
    await db.refresh(answer)
    return answer


# ─────────────────────────────────────────────────────────────────────────────
#  Apply queue
# ─────────────────────────────────────────────────────────────────────────────
#
# User flow:
#   1. POST /api/applications/queue/{job_id}
#        → creates Application row, dispatches background tailoring task
#   2. GET  /api/applications/queue
#        → list all items currently in QUEUED / TAILORING / READY_FOR_REVIEW /
#          TAILOR_FAILED (i.e. anything still mid-flow)
#   3. GET  /api/applications/queue/{app_id}
#        → fetch the tailored resume + fit summary for the review screen
#   4. PATCH /api/applications/queue/{app_id}
#        → user edits the tailored resume / cover letter before submitting
#   5. POST /api/applications/queue/{app_id}/retry
#        → re-fire the tailoring task (useful after TAILOR_FAILED)
#   6. POST /api/applications/queue/{app_id}/submit
#        → mark as APPLIED.  Optionally pass `auto=true` to fire the auto-apply
#          Playwright pipeline (best-effort, requires the company to use a
#          supported ATS like Greenhouse/Lever/Ashby).


class QueueDetailOut(BaseModel):
    id: int
    job_id: int
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    job_url: Optional[str] = None
    apply_url: Optional[str] = None
    status: str
    tailored_resume_md: Optional[str] = None
    cover_letter: Optional[str] = None
    fit_score: Optional[int] = None
    fit_summary_json: Optional[dict] = None
    tailoring_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class QueueEditIn(BaseModel):
    tailored_resume_md: Optional[str] = None
    cover_letter: Optional[str] = None
    notes: Optional[str] = None


class QueueSubmitIn(BaseModel):
    # If true, fire the auto-apply Playwright pipeline.  Best-effort: only
    # works for known ATSes (Greenhouse / Lever / Ashby).  Other employers
    # require manual submission via the apply_url.
    auto: bool = False


def _to_queue_detail(app: Application, job: Optional[Job] = None) -> QueueDetailOut:
    return QueueDetailOut(
        id=app.id,
        job_id=app.job_id,
        job_title=getattr(job, "title", None),
        company_name=getattr(job, "company_name", None),
        job_url=getattr(job, "url", None),
        apply_url=getattr(job, "apply_url", None) or getattr(job, "url", None),
        status=str(app.status),
        tailored_resume_md=app.tailored_resume_md,
        cover_letter=app.cover_letter,
        fit_score=app.fit_score,
        fit_summary_json=app.fit_summary_json,
        tailoring_error=app.tailoring_error,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


@router.post("/queue/{job_id}", response_model=QueueDetailOut, status_code=202)
async def add_to_queue(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a job to the apply queue.  Idempotent — if the user has already
    queued/started this job we return the existing row instead of creating a
    duplicate.  Dispatches the background tailoring task on first add."""
    # Confirm the job exists
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Dedup: any pre-existing Application for (user, job) means we've seen it
    existing = (await db.execute(
        select(Application).where(
            and_(Application.user_id == current_user.id, Application.job_id == job_id)
        )
    )).scalar_one_or_none()

    if existing:
        # If it was previously closed / rejected / etc., re-open it for the queue
        if existing.status in (
            ApplicationStatus.REJECTED, ApplicationStatus.CLOSED,
            ApplicationStatus.SAVED, ApplicationStatus.TAILOR_FAILED,
        ):
            existing.status = ApplicationStatus.QUEUED
            existing.tailoring_error = None
            await db.commit()
        if existing.status == ApplicationStatus.QUEUED:
            # Fire the tailoring task (idempotent on already-running task)
            from app.workers.application_tasks import tailor_application
            tailor_application.delay(existing.id)
        return _to_queue_detail(existing, job)

    # New queue entry
    app = Application(
        user_id=current_user.id,
        job_id=job_id,
        status=ApplicationStatus.QUEUED,
        platform_used=job.source or job.ats_type if hasattr(job, "ats_type") else None,
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)

    from app.workers.application_tasks import tailor_application
    tailor_application.delay(app.id)

    return _to_queue_detail(app, job)


@router.get("/queue", response_model=list[QueueDetailOut])
async def list_queue(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all items currently in the apply queue (any non-terminal status)."""
    apps = (await db.execute(
        select(Application)
        .where(
            Application.user_id == current_user.id,
            Application.status.in_(_QUEUE_STATUSES),
        )
        .order_by(desc(Application.created_at))
    )).scalars().all()
    if not apps:
        return []
    # Fetch job rows in one query
    job_ids = list({a.job_id for a in apps})
    jobs = {
        j.id: j for j in (await db.execute(
            select(Job).where(Job.id.in_(job_ids))
        )).scalars().all()
    }
    return [_to_queue_detail(a, jobs.get(a.job_id)) for a in apps]


@router.get("/queue/{app_id}", response_model=QueueDetailOut)
async def get_queue_detail(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    app = (await db.execute(
        select(Application).where(
            and_(Application.id == app_id, Application.user_id == current_user.id)
        )
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Queue item not found")
    job = (await db.execute(select(Job).where(Job.id == app.job_id))).scalar_one_or_none()
    return _to_queue_detail(app, job)


@router.patch("/queue/{app_id}", response_model=QueueDetailOut)
async def edit_queue_item(
    app_id: int,
    body: QueueEditIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Let the user edit the tailored resume or cover letter before submitting."""
    app = (await db.execute(
        select(Application).where(
            and_(Application.id == app_id, Application.user_id == current_user.id)
        )
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if body.tailored_resume_md is not None:
        app.tailored_resume_md = body.tailored_resume_md
    if body.cover_letter is not None:
        app.cover_letter = body.cover_letter
    if body.notes is not None:
        app.notes = body.notes
    await db.commit()
    await db.refresh(app)
    job = (await db.execute(select(Job).where(Job.id == app.job_id))).scalar_one_or_none()
    return _to_queue_detail(app, job)


@router.post("/queue/{app_id}/retry", response_model=QueueDetailOut)
async def retry_tailoring(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-fire the tailoring task — useful after TAILOR_FAILED."""
    app = (await db.execute(
        select(Application).where(
            and_(Application.id == app_id, Application.user_id == current_user.id)
        )
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Queue item not found")
    app.status = ApplicationStatus.QUEUED
    app.tailoring_error = None
    await db.commit()
    from app.workers.application_tasks import tailor_application
    tailor_application.delay(app.id)
    await db.refresh(app)
    job = (await db.execute(select(Job).where(Job.id == app.job_id))).scalar_one_or_none()
    return _to_queue_detail(app, job)


@router.post("/queue/{app_id}/submit", response_model=QueueDetailOut)
async def submit_queue_item(
    app_id: int,
    body: QueueSubmitIn = QueueSubmitIn(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark the queue item as APPLIED.  Manual: user submitted through the
    company's portal themselves and is just recording it here.  Auto: fires
    the auto-apply Playwright pipeline if `auto=true` and the job's ATS is
    supported — the pipeline opens the apply page in a headless browser and
    fills the form using the user's profile + the tailored resume."""
    app = (await db.execute(
        select(Application).where(
            and_(Application.id == app_id, Application.user_id == current_user.id)
        )
    )).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Queue item not found")

    if body.auto:
        # Best-effort hand-off to the auto-apply pipeline (Playwright in worker).
        # Failure here is non-fatal — user can always submit manually.
        try:
            from app.workers.auto_apply_tasks import submit_application_task
            submit_application_task.delay(app.id)
            app.status = ApplicationStatus.FORM_PENDING
        except Exception as e:
            # Auto-apply infra not wired up — fall through to manual record
            app.notes = (app.notes or "") + f"\n[auto-apply unavailable: {str(e)[:120]}]"
            app.status = ApplicationStatus.APPLIED
            app.applied_at = datetime.now(timezone.utc)
    else:
        app.status = ApplicationStatus.APPLIED
        app.applied_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(app)
    job = (await db.execute(select(Job).where(Job.id == app.job_id))).scalar_one_or_none()
    return _to_queue_detail(app, job)
