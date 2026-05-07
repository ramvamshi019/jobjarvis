"""Recruiter outreach endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.job import Job
from app.models.application import OutreachMessage
from app.models.ai_models import AIDecision
from app.ai.recruiter_writer import generate_outreach

router = APIRouter(tags=["outreach"])


@router.post("/jobs/{job_id}/generate-outreach")
async def generate_outreach_for_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Get decision for context
    dec_q = await db.execute(
        select(AIDecision).where(
            and_(AIDecision.job_id == job_id, AIDecision.user_id == current_user.id)
        ).limit(1)
    )
    decision = dec_q.scalar_one_or_none()

    user_profile = {"full_name": current_user.full_name or "Candidate"}
    job_dict = {
        "id": job.id, "title": job.title, "company_name": job.company_name,
        "role_category": job.role_category or "Data/AI Engineer",
    }

    bundle = generate_outreach(
        job=job_dict,
        user_profile=user_profile,
        matched_skills=decision.matched_skills if decision else [],
        missing_skills=decision.missing_skills if decision else [],
    )

    # Persist messages
    message_types = [
        ("recruiter_email", bundle.recruiter_email_subject, bundle.recruiter_email_body),
        ("linkedin_dm", None, bundle.linkedin_dm),
        ("cover_letter", None, bundle.cover_letter),
        ("follow_up_3d", None, bundle.follow_up_3d),
        ("follow_up_7d", None, bundle.follow_up_7d),
    ]
    for msg_type, subject, body in message_types:
        msg = OutreachMessage(
            job_id=job_id,
            user_id=current_user.id,
            message_type=msg_type,
            subject=subject,
            body=body,
        )
        db.add(msg)

    await db.commit()
    return {
        "job_id": job_id,
        "recruiter_email_subject": bundle.recruiter_email_subject,
        "recruiter_email_body": bundle.recruiter_email_body,
        "linkedin_dm": bundle.linkedin_dm,
        "cover_letter": bundle.cover_letter,
        "follow_up_3d": bundle.follow_up_3d,
        "follow_up_7d": bundle.follow_up_7d,
    }


@router.get("/outreach/messages")
async def list_outreach_messages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(OutreachMessage).where(OutreachMessage.user_id == current_user.id)
    )
    messages = result.scalars().all()
    return [
        {
            "id": m.id, "job_id": m.job_id, "message_type": m.message_type,
            "subject": m.subject, "body": m.body[:200] + "..." if len(m.body) > 200 else m.body,
            "status": m.status, "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]
