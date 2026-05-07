"""Reports and export endpoints."""
import csv
import io
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.job import Job
from app.models.application import Application
from app.services.market_trends import get_market_trends
from app.ai.learning_engine import compute_skill_gaps

router = APIRouter(tags=["reports"])


@router.get("/reports/weekly-market")
async def weekly_market_report(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await get_market_trends(db, days=7)


@router.get("/reports/skill-gaps")
async def skill_gap_report(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.resume import ResumeVersion
    resume_q = await db.execute(
        select(ResumeVersion).where(
            and_(ResumeVersion.user_id == current_user.id, ResumeVersion.is_active == True)
        )
    )
    resume = resume_q.scalar_one_or_none()
    skills = []
    if resume and resume.skills_json:
        skills = resume.skills_json.get("all", [])

    gaps = await compute_skill_gaps(db, current_user.id, skills)
    return [
        {
            "skill": g.skill,
            "importance": g.importance,
            "frequency": g.frequency,
            "learning_plan": g.learning_plan,
            "project_suggestion": g.project_suggestion,
            "estimated_days": g.estimated_days,
            "resume_tip": g.resume_tip,
        }
        for g in gaps
    ]


@router.get("/export/jobs.csv")
async def export_jobs_csv(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Job).where(Job.active == True).order_by(Job.first_seen_at.desc()).limit(5000)
    )
    jobs = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "title", "company", "location", "country", "remote_type",
        "role_category", "experience_level", "salary_min", "salary_max",
        "source_type", "spam_score", "freshness", "first_seen_at", "job_url",
    ])
    for j in jobs:
        writer.writerow([
            j.id, j.title, j.company_name, j.normalized_location, j.country,
            j.remote_type, j.role_category, j.experience_level,
            j.salary_min, j.salary_max, j.source_type, j.spam_score,
            j.freshness_label, j.first_seen_at, j.job_url,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs.csv"},
    )


@router.get("/export/applications.csv")
async def export_applications_csv(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Application).where(Application.user_id == current_user.id)
    )
    apps = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "job_id", "status", "applied_at", "follow_up_at",
        "recruiter_name", "recruiter_email", "outcome", "interview_rounds",
    ])
    for a in apps:
        writer.writerow([
            a.id, a.job_id, a.status,
            a.applied_at.isoformat() if a.applied_at else "",
            a.follow_up_at.isoformat() if a.follow_up_at else "",
            a.recruiter_name or "", a.recruiter_email or "",
            a.outcome or "", a.interview_rounds,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications.csv"},
    )


@router.get("/observability/summary")
async def observability_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.observability import get_observability_summary
    return await get_observability_summary(db)
