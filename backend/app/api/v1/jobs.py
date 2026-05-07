"""Job endpoints — browse, filter, analyze."""
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc, func

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.job import Job
from app.models.ai_models import AIDecision
from app.schemas.job import JobOut, JobFilter
from app.ai.role_classifier import classify_role
from app.ai.skill_extractor import extract_skills
from app.ai.spam_detector import detect_spam
from app.ai.source_classifier import classify_source
from app.ai.work_auth_detector import detect_work_auth

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_filter_query(q, f: JobFilter):
    if f.role_category:
        roles = [r.strip() for r in f.role_category.split(",")]
        q = q.where(Job.role_category.in_(roles))
    if f.country:
        q = q.where(Job.country == f.country)
    if f.remote_type:
        q = q.where(Job.remote_type == f.remote_type)
    if f.experience_level:
        q = q.where(Job.experience_level == f.experience_level)
    if f.min_salary:
        q = q.where(Job.salary_max >= f.min_salary)
    if f.freshness:
        q = q.where(Job.freshness_label == f.freshness)
    if f.source_type:
        q = q.where(Job.source_type == f.source_type)
    return q


@router.get("", response_model=list[JobOut])
async def list_jobs(
    role_category: Optional[str] = None,
    country: Optional[str] = None,
    remote_type: Optional[str] = None,
    timezone: Optional[str] = None,
    experience_level: Optional[str] = None,
    min_salary: Optional[int] = None,
    source_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    f = JobFilter(
        role_category=role_category, country=country, remote_type=remote_type,
        experience_level=experience_level, min_salary=min_salary, source_type=source_type,
        timezone=timezone, page=page, page_size=page_size,
    )
    q = select(Job).where(Job.active == True)
    q = _job_filter_query(q, f)
    # Priority sorting: recent, high confidence
    q = q.order_by(desc(Job.first_seen_at), desc(Job.source_confidence))
    # We fetch more jobs to rank them by intelligence
    q = q.limit(100)
    result = await db.execute(q)
    jobs = list(result.scalars().all())
    
    from app.ai.decision_engine import evaluate_job_decision
    
    # Pre-evaluate decisions for the subset
    enriched_jobs = []
    for job in jobs:
        dec = await evaluate_job_decision(db, job, current_user)
        job.decision = dec.decision
        job.fit_score = dec.fit_score
        enriched_jobs.append(job)
        
    # Rank by intelligence (fit_score)
    enriched_jobs.sort(key=lambda x: x.fit_score or 0, reverse=True)
    
    # Paginate manually after ranking
    start = (page - 1) * page_size
    end = start + page_size
    return enriched_jobs[start:end]


from app.schemas.decision import DecisionOut
from app.ai.decision_engine import evaluate_job_decision

@router.get("/{job_id}/decision", response_model=DecisionOut)
async def get_job_decision(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    decision = await evaluate_job_decision(db, job, current_user)
    return decision


@router.get("/fresh", response_model=list[JobOut])
async def fresh_jobs(
    hours: int = Query(24, ge=1, le=168),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Jobs posted/seen in the last N hours (default 24 h). No auth required."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = (
        select(Job)
        .where(and_(Job.active == True, Job.first_seen_at >= cutoff))
        .order_by(desc(Job.first_seen_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(q)
    return list(result.scalars().all())


@router.get("/new", response_model=list[JobOut])
async def new_jobs(
    hours: int = Query(24, ge=1, le=168),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = (
        select(Job)
        .where(and_(Job.active == True, Job.first_seen_at >= cutoff))
        .order_by(desc(Job.first_seen_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(q)
    return list(result.scalars().all())


@router.get("/ai-data", response_model=list[JobOut])
async def ai_data_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ai_roles = ["AI Engineer", "ML Engineer", "Data Engineer", "Data Platform Engineer",
                "MLOps Engineer", "Analytics Engineer"]
    q = (
        select(Job)
        .where(and_(Job.active == True, Job.role_category.in_(ai_roles)))
        .order_by(desc(Job.first_seen_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(q)
    return list(result.scalars().all())


@router.get("/apply-now", response_model=list[dict])
async def apply_now_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        select(AIDecision, Job)
        .join(Job, Job.id == AIDecision.job_id)
        .where(and_(
            AIDecision.user_id == current_user.id,
            AIDecision.decision == "APPLY_NOW",
            Job.active == True,
        ))
        .order_by(desc(AIDecision.fit_score))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(q)
    rows = result.all()
    return [
        {
            "decision_id": dec.id,
            "job_id": job.id,
            "title": job.title,
            "company": job.company_name,
            "location": job.normalized_location,
            "fit_score": dec.fit_score,
            "apply_within_hours": dec.apply_within_hours,
            "priority": dec.priority,
            "matched_skills": dec.matched_skills,
            "missing_skills": dec.missing_skills,
            "freshness": job.freshness_label,
        }
        for dec, job in rows
    ]


@router.get("/decisions", response_model=list[dict])
async def all_decisions(
    decision_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(AIDecision).where(AIDecision.user_id == current_user.id)
    if decision_type:
        q = q.where(AIDecision.decision == decision_type)
    q = q.order_by(desc(AIDecision.created_at)).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    decisions = result.scalars().all()
    return [
        {
            "id": d.id, "job_id": d.job_id, "decision": d.decision,
            "fit_score": d.fit_score, "priority": d.priority,
            "confidence": d.confidence, "role_category": d.role_category,
            "needs_human_review": d.needs_human_review,
            "created_at": d.created_at.isoformat(),
        }
        for d in decisions
    ]


@router.get("/search", response_model=list[JobOut])
async def search_jobs(
    q: str = Query(..., min_length=2),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        select(Job)
        .where(
            and_(
                Job.active == True,
                (Job.title.ilike(f"%{q}%")) | (Job.company_name.ilike(f"%{q}%"))
            )
        )
        .order_by(desc(Job.first_seen_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/{job_id}/analyze")
async def analyze_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run AI analysis on a specific job."""
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Run classifiers
    # NOTE: Do NOT access job.company here — lazy-loading relationships
    # triggers a synchronous DB call that fails in async context.
    role_cls = classify_role(job.title, job.description or "")
    skills = extract_skills(job.title, job.description or "")
    spam = detect_spam({
        "title": job.title,
        "description": job.description or "",
        "company_domain": None,  # company relationship not loaded; use None safely
    })
    work_auth = detect_work_auth(job.description or "", job.title)

    # Update job
    job.role_category = role_cls.role_category
    job.role_confidence = role_cls.confidence_score
    job.required_skills = skills.required_skills
    job.preferred_skills = skills.preferred_skills
    job.matched_tools = skills.all_skills
    job.spam_score = spam.spam_score
    job.spam_flags_json = {"flags": spam.spam_flags}
    job.eligibility_risk_score = work_auth.eligibility_risk_score
    job.work_auth_flags_json = {"flags": work_auth.work_auth_flags}

    await db.commit()
    return {
        "job_id": job_id,
        "role_category": role_cls.role_category,
        "role_confidence": role_cls.confidence_score,
        "required_skills": skills.required_skills,
        "preferred_skills": skills.preferred_skills,
        "spam_score": spam.spam_score,
        "spam_flags": spam.spam_flags,
        "work_auth_flags": work_auth.work_auth_flags,
        "eligibility_risk": work_auth.eligibility_risk_score,
    }


@router.get("/{job_id}/decision")
async def get_job_decision(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(AIDecision).where(
            and_(AIDecision.job_id == job_id, AIDecision.user_id == current_user.id)
        ).order_by(desc(AIDecision.created_at)).limit(1)
    )
    decision = result.scalar_one_or_none()
    if not decision:
        raise HTTPException(status_code=404, detail="No AI decision for this job")
    return decision
