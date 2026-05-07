"""Market intelligence & analytics API.

Endpoints (all public, no auth required):
  GET /analytics/skills/trending        — top skills over last N days
  GET /analytics/skills/gap             — skills missing vs. job demand
  GET /analytics/companies/hiring       — most actively hiring companies
  GET /analytics/companies/spikes       — companies with hiring spikes
  GET /analytics/market/overview        — aggregate market stats
  GET /analytics/market/trends          — daily job volume trends
  GET /analytics/salary/insights        — salary ranges by role/location
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.company import Company

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ── Skills: trending ─────────────────────────────────────────────────────────

class SkillCount(BaseModel):
    skill: str
    count: int
    pct_of_jobs: float


class TrendingSkillsResponse(BaseModel):
    skills: list[SkillCount]
    window_days: int
    total_jobs_analyzed: int
    as_of: str


@router.get("/skills/trending", response_model=TrendingSkillsResponse)
async def trending_skills(
    days: int = Query(30, ge=1, le=365, description="Lookback window in days"),
    top_k: int = Query(25, ge=1, le=100),
    role: Optional[str] = Query(None, description="Filter by role category"),
    db: AsyncSession = Depends(get_db),
):
    """
    Which skills appear most frequently in job postings over the last N days.
    Useful for: understanding what the market actually wants right now.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Use PostgreSQL's jsonb_array_elements_text to unnest required_skills JSON
    filters = "j.active = true AND j.first_seen_at >= :cutoff AND j.required_skills IS NOT NULL"
    role_filter = ""
    if role:
        role_filter = "AND j.role_category ILIKE :role"

    sql = text(f"""
        SELECT skill, COUNT(*) AS cnt
        FROM jobs j,
             jsonb_array_elements_text(j.required_skills::jsonb) AS skill
        WHERE {filters} {role_filter}
        GROUP BY skill
        ORDER BY cnt DESC
        LIMIT :top_k
    """)

    params = {"cutoff": cutoff, "top_k": top_k}
    if role:
        params["role"] = f"%{role}%"

    try:
        result = await db.execute(sql, params)
        rows = result.fetchall()
    except Exception:
        rows = []

    # Total jobs in window for percentage calculation
    total_q = await db.execute(
        select(func.count(Job.id)).where(
            and_(Job.active == True, Job.first_seen_at >= cutoff)
        )
    )
    total = total_q.scalar() or 1

    skills = [
        SkillCount(
            skill=row[0],
            count=row[1],
            pct_of_jobs=round(row[1] / total * 100, 1),
        )
        for row in rows
    ]

    return TrendingSkillsResponse(
        skills=skills,
        window_days=days,
        total_jobs_analyzed=total,
        as_of=datetime.now(timezone.utc).isoformat(),
    )


# ── Skills: gap analysis ──────────────────────────────────────────────────────

class SkillGap(BaseModel):
    skill: str
    jobs_requiring: int
    pct_of_jobs: float
    in_your_profile: bool


class SkillGapResponse(BaseModel):
    gaps: list[SkillGap]
    matched: list[SkillGap]
    match_rate: float
    days: int


@router.get("/skills/gap", response_model=SkillGapResponse)
async def skill_gap(
    your_skills: str = Query(..., description="Comma-separated skills you have"),
    role: Optional[str] = Query("Data Engineer", description="Target role"),
    days: int = Query(30, ge=7, le=90),
    db: AsyncSession = Depends(get_db),
):
    """
    Compare your skills against what the market demands for a role.
    Shows which skills you're missing and how often they appear in job postings.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    user_skills = {s.strip().lower() for s in your_skills.split(",") if s.strip()}

    sql = text("""
        SELECT skill, COUNT(*) AS cnt
        FROM jobs j,
             jsonb_array_elements_text(j.required_skills::jsonb) AS skill
        WHERE j.active = true
          AND j.first_seen_at >= :cutoff
          AND j.role_category ILIKE :role
          AND j.required_skills IS NOT NULL
        GROUP BY skill
        ORDER BY cnt DESC
        LIMIT 40
    """)

    try:
        result = await db.execute(sql, {"cutoff": cutoff, "role": f"%{role}%"})
        rows = result.fetchall()
    except Exception:
        rows = []

    total_q = await db.execute(
        select(func.count(Job.id)).where(
            and_(
                Job.active == True,
                Job.first_seen_at >= cutoff,
                Job.role_category.ilike(f"%{role}%"),
            )
        )
    )
    total = total_q.scalar() or 1

    gaps, matched = [], []
    for skill, cnt in rows:
        item = SkillGap(
            skill=skill,
            jobs_requiring=cnt,
            pct_of_jobs=round(cnt / total * 100, 1),
            in_your_profile=skill.lower() in user_skills,
        )
        if item.in_your_profile:
            matched.append(item)
        else:
            gaps.append(item)

    all_skills = matched + gaps
    match_rate = round(len(matched) / max(len(all_skills), 1) * 100, 1)

    return SkillGapResponse(
        gaps=gaps,
        matched=matched,
        match_rate=match_rate,
        days=days,
    )


# ── Companies: most actively hiring ──────────────────────────────────────────

class HiringCompany(BaseModel):
    company_id: int
    company_name: str
    ats_type: Optional[str]
    jobs_count: int
    last_posted: Optional[str]


class HiringCompaniesResponse(BaseModel):
    companies: list[HiringCompany]
    window_days: int
    as_of: str


@router.get("/companies/hiring", response_model=HiringCompaniesResponse)
async def most_hiring_companies(
    days: int = Query(7, ge=1, le=90),
    top_k: int = Query(25, ge=1, le=100),
    role: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Companies posting the most jobs recently — useful for targeting outreach."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    q = (
        select(
            Job.company_id,
            Job.company_name,
            Company.ats_type,
            func.count(Job.id).label("jobs_count"),
            func.max(Job.first_seen_at).label("last_posted"),
        )
        .join(Company, Company.id == Job.company_id, isouter=True)
        .where(and_(Job.active == True, Job.first_seen_at >= cutoff))
    )

    if role:
        q = q.where(Job.role_category.ilike(f"%{role}%"))

    q = q.group_by(Job.company_id, Job.company_name, Company.ats_type)
    q = q.order_by(desc("jobs_count")).limit(top_k)

    result = await db.execute(q)
    rows = result.fetchall()

    companies = [
        HiringCompany(
            company_id=row.company_id,
            company_name=row.company_name,
            ats_type=row.ats_type,
            jobs_count=row.jobs_count,
            last_posted=row.last_posted.isoformat() if row.last_posted else None,
        )
        for row in rows
    ]

    return HiringCompaniesResponse(
        companies=companies,
        window_days=days,
        as_of=datetime.now(timezone.utc).isoformat(),
    )


# ── Companies: hiring spikes ──────────────────────────────────────────────────

@router.get("/companies/spikes")
async def hiring_spikes(
    db: AsyncSession = Depends(get_db),
):
    """Companies with unusual hiring velocity (detected by z-score anomaly detection)."""
    from app.models.ai_models import CompanyIntelligence

    result = await db.execute(
        select(Company, CompanyIntelligence)
        .join(CompanyIntelligence, CompanyIntelligence.company_id == Company.id)
        .where(
            and_(
                Company.active == True,
                CompanyIntelligence.hiring_velocity > 5,
            )
        )
        .order_by(desc(CompanyIntelligence.hiring_velocity))
        .limit(20)
    )
    rows = result.fetchall()

    return {
        "spikes": [
            {
                "company_id": company.id,
                "company_name": company.name,
                "ats_type": company.ats_type,
                "hiring_velocity": intel.hiring_velocity,
                "jobs_last_7_days": intel.jobs_last_7_days,
                "jobs_last_30_days": intel.jobs_last_30_days,
            }
            for company, intel in rows
        ],
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# ── Market overview ───────────────────────────────────────────────────────────

@router.get("/market/overview")
async def market_overview(db: AsyncSession = Depends(get_db)):
    """High-level market snapshot: total jobs, ATS distribution, role breakdown."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    week_ago  = now - timedelta(days=7)

    total_q = await db.execute(
        select(func.count(Job.id)).where(Job.active == True)
    )
    last_24h_q = await db.execute(
        select(func.count(Job.id)).where(
            and_(Job.active == True, Job.first_seen_at >= yesterday)
        )
    )
    last_7d_q = await db.execute(
        select(func.count(Job.id)).where(
            and_(Job.active == True, Job.first_seen_at >= week_ago)
        )
    )
    companies_q = await db.execute(
        select(func.count(Company.id)).where(Company.active == True)
    )

    # ATS distribution
    ats_q = await db.execute(
        select(Company.ats_type, func.count(Company.id))
        .where(Company.active == True)
        .group_by(Company.ats_type)
        .order_by(desc(func.count(Company.id)))
    )
    ats_dist = {row[0] or "unknown": row[1] for row in ats_q.fetchall()}

    # Role distribution (top 10)
    role_q = await db.execute(
        select(Job.role_category, func.count(Job.id))
        .where(and_(Job.active == True, Job.role_category != None))
        .group_by(Job.role_category)
        .order_by(desc(func.count(Job.id)))
        .limit(10)
    )
    role_dist = {row[0]: row[1] for row in role_q.fetchall()}

    # Remote type breakdown
    remote_q = await db.execute(
        select(Job.remote_type, func.count(Job.id))
        .where(Job.active == True)
        .group_by(Job.remote_type)
    )
    remote_dist = {row[0] or "unknown": row[1] for row in remote_q.fetchall()}

    return {
        "total_active_jobs": total_q.scalar() or 0,
        "jobs_last_24h": last_24h_q.scalar() or 0,
        "jobs_last_7d": last_7d_q.scalar() or 0,
        "total_companies": companies_q.scalar() or 0,
        "ats_distribution": ats_dist,
        "role_distribution": role_dist,
        "remote_distribution": remote_dist,
        "as_of": now.isoformat(),
    }


# ── Market daily trends ───────────────────────────────────────────────────────

class DailyTrend(BaseModel):
    date: str
    jobs_posted: int
    new_companies: int


@router.get("/market/trends")
async def market_trends(
    days: int = Query(30, ge=7, le=180),
    role: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Daily job posting volume over the last N days."""
    now = datetime.now(timezone.utc)
    trends = []

    for i in range(days, 0, -1):
        day_start = now - timedelta(days=i)
        day_end   = now - timedelta(days=i - 1)

        job_q = select(func.count(Job.id)).where(
            and_(
                Job.active == True,
                Job.first_seen_at >= day_start,
                Job.first_seen_at < day_end,
            )
        )
        if role:
            job_q = job_q.where(Job.role_category.ilike(f"%{role}%"))

        company_q = select(func.count(Company.id)).where(
            and_(
                Company.created_at >= day_start,
                Company.created_at < day_end,
            )
        )

        jobs_r = await db.execute(job_q)
        comp_r = await db.execute(company_q)

        trends.append(DailyTrend(
            date=day_start.strftime("%Y-%m-%d"),
            jobs_posted=jobs_r.scalar() or 0,
            new_companies=comp_r.scalar() or 0,
        ))

    return {"trends": [t.model_dump() for t in trends], "days": days, "role": role}


# ── Salary insights ───────────────────────────────────────────────────────────

@router.get("/salary/insights")
async def salary_insights(
    role: Optional[str] = Query("Data Engineer"),
    country: Optional[str] = Query("US"),
    db: AsyncSession = Depends(get_db),
):
    """Salary range statistics for a role/location combination."""
    filters = [
        Job.active == True,
        Job.salary_min != None,
        Job.salary_max != None,
        Job.salary_min > 0,
        Job.salary_currency == "USD",
    ]
    if role:
        filters.append(Job.role_category.ilike(f"%{role}%"))
    if country:
        filters.append(Job.country == country.upper())

    result = await db.execute(
        select(
            func.avg(Job.salary_min).label("avg_min"),
            func.avg(Job.salary_max).label("avg_max"),
            func.percentile_cont(0.25).within_group(Job.salary_min).label("p25"),
            func.percentile_cont(0.50).within_group(Job.salary_min).label("p50"),
            func.percentile_cont(0.75).within_group(Job.salary_min).label("p75"),
            func.min(Job.salary_min).label("min_val"),
            func.max(Job.salary_max).label("max_val"),
            func.count(Job.id).label("sample_size"),
        ).where(and_(*filters))
    )
    row = result.fetchone()

    # Per-experience-level breakdown
    exp_q = await db.execute(
        select(
            Job.experience_level,
            func.avg(Job.salary_min).label("avg_min"),
            func.avg(Job.salary_max).label("avg_max"),
            func.count(Job.id).label("count"),
        )
        .where(and_(*filters, Job.experience_level != None))
        .group_by(Job.experience_level)
        .order_by(desc("count"))
    )
    by_level = {
        r.experience_level: {
            "avg_min": int(r.avg_min or 0),
            "avg_max": int(r.avg_max or 0),
            "count": r.count,
        }
        for r in exp_q.fetchall()
    }

    def _i(v):
        return int(v) if v is not None else None

    return {
        "role": role,
        "country": country,
        "overall": {
            "avg_min": _i(row.avg_min),
            "avg_max": _i(row.avg_max),
            "p25": _i(row.p25),
            "p50": _i(row.p50),
            "p75": _i(row.p75),
            "min": _i(row.min_val),
            "max": _i(row.max_val),
            "sample_size": row.sample_size,
        },
        "by_experience_level": by_level,
        "currency": "USD",
        "period": "annual",
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
