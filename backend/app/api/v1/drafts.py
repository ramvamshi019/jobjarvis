"""
Application-draft review endpoints.

The auto-apply agent runs in dry-run mode and saves drafts to the
auto_apply_runs table.  This API lets the user list pending drafts, edit
the AI-generated answers, and submit the final application.
"""
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/drafts", tags=["drafts"])


class DraftAnswer(BaseModel):
    question: str
    answer: str


class DraftOut(BaseModel):
    id: int
    job_id: int
    job_title: str
    company: str
    apply_url: Optional[str]
    ats: Optional[str]
    fields_filled: int
    answers: list[DraftAnswer]
    screenshot: Optional[str]
    created_at: str
    status: str


@router.get("", response_model=list[DraftOut])
async def list_drafts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List pending auto-apply drafts for the current user."""
    sql = text(
        """
        SELECT r.id, r.job_id, r.ats, r.fields_filled, r.drafts_json,
               r.screenshot, r.created_at, r.status,
               j.title, j.company_name, COALESCE(j.apply_url, j.url) AS apply_url
        FROM auto_apply_runs r
        JOIN jobs j ON j.id = r.job_id
        WHERE r.user_id = :uid AND r.status = 'pending'
        ORDER BY r.created_at DESC
        LIMIT 50
        """
    )
    res = await db.execute(sql, {"uid": current_user.id})
    out = []
    for r in res.mappings().all():
        drafts_raw = r["drafts_json"] or []
        if isinstance(drafts_raw, str):
            try: drafts_raw = json.loads(drafts_raw)
            except Exception: drafts_raw = []
        out.append(DraftOut(
            id=r["id"],
            job_id=r["job_id"],
            job_title=r["title"],
            company=r["company_name"] or "",
            apply_url=r["apply_url"],
            ats=r["ats"],
            fields_filled=r["fields_filled"] or 0,
            answers=[DraftAnswer(**d) for d in drafts_raw],
            screenshot=r["screenshot"],
            created_at=r["created_at"].isoformat(),
            status=r["status"],
        ))
    return out


class UpdateDraftRequest(BaseModel):
    answers: list[DraftAnswer]


@router.patch("/{draft_id}", response_model=DraftOut)
async def update_draft(
    draft_id: int,
    body: UpdateDraftRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save user's edits to the draft answers."""
    new_json = [a.model_dump() for a in body.answers]
    res = await db.execute(
        text(
            "UPDATE auto_apply_runs "
            "SET drafts_json = :drafts::jsonb "
            "WHERE id = :id AND user_id = :uid "
            "RETURNING id"
        ),
        {"id": draft_id, "uid": current_user.id, "drafts": json.dumps(new_json)},
    )
    if res.first() is None:
        raise HTTPException(404, "Draft not found")
    await db.commit()
    # Re-fetch and return
    drafts = await list_drafts(db, current_user)
    for d in drafts:
        if d.id == draft_id:
            return d
    raise HTTPException(404, "Draft vanished")


@router.post("/{draft_id}/discard")
async def discard_draft(
    draft_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a draft as discarded (skip applying)."""
    res = await db.execute(
        text(
            "UPDATE auto_apply_runs SET status='discarded' "
            "WHERE id=:id AND user_id=:uid RETURNING id"
        ),
        {"id": draft_id, "uid": current_user.id},
    )
    if res.first() is None:
        raise HTTPException(404, "Draft not found")
    await db.commit()
    return {"status": "discarded"}


@router.post("/{draft_id}/submit")
async def submit_draft(
    draft_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Run auto-apply again for this job with the user's edited answers, this
    time submitting for real.  Returns the new run result.
    """
    # Fetch the draft
    res = await db.execute(
        text(
            "SELECT r.job_id, r.drafts_json, j.apply_url, j.url, "
            "       j.title, j.company_name "
            "FROM auto_apply_runs r JOIN jobs j ON j.id=r.job_id "
            "WHERE r.id=:id AND r.user_id=:uid"
        ),
        {"id": draft_id, "uid": current_user.id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(404, "Draft not found")

    # Mark this draft as submitted (we'll re-run with dry_run=false)
    await db.execute(
        text("UPDATE auto_apply_runs SET status='submitted' WHERE id=:id"),
        {"id": draft_id},
    )
    await db.commit()

    # Dispatch the real-submit via Celery
    from app.workers.auto_apply_tasks import run_one
    # Pull the user's profile
    profile = {
        "full_name": current_user.full_name or "",
        "email":     current_user.email,
        "linkedin":  current_user.linkedin_url or "",
        "github":    current_user.github_url or "",
        "portfolio": current_user.portfolio_url or "",
        "location":  current_user.current_location or "",
        "work_authorization": current_user.work_authorization or "Yes",
    }
    run_one.delay(
        user_id=current_user.id,
        user_email=current_user.email,
        user_name=current_user.full_name or "Applicant",
        resume_path="",  # backend doesn't have file; worker reads from upload dir
        resume_text="",
        job_id=row["job_id"],
        job_url=row["apply_url"] or row["url"] or "",
        company=row["company_name"] or "",
        title=row["title"],
        dry_run=False,
        profile=profile,
    )
    return {"queued": True, "draft_id": draft_id, "company": row["company_name"]}
