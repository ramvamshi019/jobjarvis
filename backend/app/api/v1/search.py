"""
Public job search API — no authentication required.
GET /api/jobs/search
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.company import Company

router = APIRouter(prefix="/jobs", tags=["public-search"])


async def _trigger_stale_scans(q: Optional[str], db: AsyncSession):
    """Fire-and-forget: trigger scans for companies matching the query that are stale."""
    try:
        from app.workers.scan_tasks import run_company_scan_task
        stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

        company_q = select(Company).where(
            and_(
                Company.active == True,
                Company.ats_type != None,
                Company.ats_identifier != None,
                or_(
                    Company.last_checked_at == None,
                    Company.last_checked_at <= stale_cutoff,
                ),
            )
        )
        if q and q.strip():
            kw = f"%{q.strip().lower()}%"
            company_q = company_q.where(Company.name.ilike(kw))

        company_q = company_q.order_by(Company.priority_score.desc()).limit(10)
        result = await db.execute(company_q)
        companies = list(result.scalars().all())

        for company in companies:
            run_company_scan_task.apply_async(
                args=[company.id],
                countdown=1,
                expires=300,     # discard if not picked up in 5 min
            )
    except Exception:
        pass   # never block search for background trigger failures

# Common country name → ISO-2 mapping for user-friendly location search
_COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "united states": "US", "usa": "US", "us": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "britain": "GB", "england": "GB",
    "canada": "CA", "germany": "DE", "france": "FR", "australia": "AU",
    "india": "IN", "netherlands": "NL", "singapore": "SG", "switzerland": "CH",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "spain": "ES", "italy": "IT", "poland": "PL", "ireland": "IE",
    "brazil": "BR", "mexico": "MX", "japan": "JP", "south korea": "KR",
    "new zealand": "NZ", "portugal": "PT", "austria": "AT", "belgium": "BE",
    "israel": "IL", "uae": "AE", "united arab emirates": "AE",
}

def _resolve_country(value: str) -> str:
    """Return ISO-2 code if value matches a known country name, else return as-is."""
    return _COUNTRY_NAME_TO_ISO2.get(value.strip().lower(), value.strip().upper())


# ── Response schema ────────────────────────────────────────────────────────────

class PublicJob(BaseModel):
    id: int
    title: str
    company_name: str
    location: Optional[str]
    remote_type: Optional[str]
    experience_level: Optional[str]
    employment_type: Optional[str]
    role_category: Optional[str]
    salary_min: Optional[int]
    salary_max: Optional[int]
    salary_currency: Optional[str]
    job_url: Optional[str]
    posted_at: Optional[datetime]
    first_seen_at: datetime
    freshness_label: Optional[str]
    freshness_score: Optional[float]
    source: Optional[str]
    required_skills: Optional[list]
    country: Optional[str]
    city: Optional[str]

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    jobs: list[PublicJob]
    total: int
    page: int
    page_size: int
    has_more: bool


# ── Search endpoint ────────────────────────────────────────────────────────────

@router.get("/search", response_model=SearchResponse)
async def search_jobs(
    background_tasks: BackgroundTasks,
    q: Optional[str] = Query(None, description="Keyword (title or company)"),
    location: Optional[str] = Query(None, description="City, country, or 'remote'"),
    experience: Optional[str] = Query(None, description="entry|mid|senior|lead|staff"),
    remote: Optional[str] = Query(None, description="remote|hybrid|onsite"),
    role: Optional[str] = Query(None, description="Data Engineer, ML Engineer, …"),
    freshness: Optional[str] = Query(None, description="last_24h|last_7_days"),
    country: Optional[str] = Query(None, description="ISO-2 country code, e.g. US"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Public full-text job search — no login required.
    Results ordered by recency (posted_at DESC, first_seen_at DESC).
    """
    filters = [Job.active == True]

    # ── Keyword search ─────────────────────────────────────────────────────
    # Tokenized: every word in the query must appear somewhere in the title
    # (any order), so "data engineer" also matches "Senior Data Platform
    # Engineer". The whole phrase still matches a company name. Falls back to
    # plain substring for single short tokens.
    if q and q.strip():
        raw = q.strip()
        tokens = [t for t in re.split(r"\s+", raw) if len(t) >= 2]
        if len(tokens) >= 2:
            token_clauses = [
                or_(
                    Job.title.ilike(f"%{tok}%"),
                    Job.normalized_title.ilike(f"%{tok}%"),
                )
                for tok in tokens
            ]
            filters.append(
                or_(
                    and_(*token_clauses),
                    Job.company_name.ilike(f"%{raw}%"),
                )
            )
        else:
            kw = f"%{raw}%"
            filters.append(
                or_(
                    Job.title.ilike(kw),
                    Job.company_name.ilike(kw),
                    Job.normalized_title.ilike(kw),
                )
            )

    # ── Location filter ────────────────────────────────────────────────────
    if location and location.strip():
        loc = location.strip().lower()
        if loc in ("remote", "wfh", "work from home"):
            filters.append(Job.remote_type == "remote")
        else:
            # Try to resolve full country name → ISO-2 first
            iso2 = _COUNTRY_NAME_TO_ISO2.get(loc)
            if iso2:
                # Matched a country name — filter by country code
                filters.append(Job.country == iso2)
            else:
                loc_like = f"%{loc}%"
                filters.append(
                    or_(
                        Job.city.ilike(loc_like),
                        Job.country.ilike(loc_like),
                        Job.location.ilike(loc_like),
                        Job.normalized_location.ilike(loc_like),
                    )
                )

    # ── Experience filter ──────────────────────────────────────────────────
    if experience:
        filters.append(Job.experience_level == experience.lower())

    # ── Remote type filter ─────────────────────────────────────────────────
    if remote:
        filters.append(Job.remote_type == remote.lower())

    # ── Role category filter ───────────────────────────────────────────────
    # role_category is sparsely/under-populated by the classifier, so a chip
    # like "Data Engineer" would only surface a handful of jobs if we gated
    # on role_category alone. Fall back to a title match so the UI role chips
    # reflect the true volume even for un-classified rows.
    if role and role.strip():
        r = role.strip()
        filters.append(
            or_(
                Job.role_category.ilike(f"%{r}%"),
                Job.title.ilike(f"%{r}%"),
                Job.normalized_title.ilike(f"%{r}%"),
            )
        )

    # ── Freshness filter ───────────────────────────────────────────────────
    # Intentionally generous: a job passes if we discovered it recently OR it
    # was posted recently. Gating strictly on posted_at would collapse the
    # feed, because most sources report a stale/absent posted_at on roles
    # that are still active and being re-listed. Label honesty (showing the
    # real "Older" age on the card) is handled separately at ingest via
    # freshness_label, so the feed can stay full without lying about age.
    def _freshness_filter(cutoff):
        return or_(
            Job.first_seen_at >= cutoff,
            and_(Job.posted_at.isnot(None), Job.posted_at >= cutoff),
        )

    if freshness == "last_24h":
        filters.append(_freshness_filter(datetime.now(timezone.utc) - timedelta(hours=24)))
    elif freshness == "last_7_days":
        filters.append(_freshness_filter(datetime.now(timezone.utc) - timedelta(days=7)))
    elif freshness == "last_30_days" or freshness is None:
        # Default: only show jobs from the last 30 days
        filters.append(_freshness_filter(datetime.now(timezone.utc) - timedelta(days=30)))

    # ── Country filter ─────────────────────────────────────────────────────
    if country:
        filters.append(Job.country == _resolve_country(country))

    # ── Count query ────────────────────────────────────────────────────────
    count_q = select(func.count(Job.id)).where(and_(*filters))
    total_result = await db.execute(count_q)
    total = total_result.scalar() or 0

    # ── Data query ─────────────────────────────────────────────────────────
    # Sort: newest-ingested first, with freshness_score / posted_at as
    # tiebreakers.  Putting first_seen_at first guarantees jobs we just
    # discovered today land at the top of the list, regardless of how
    # the company stamped their own posted_at (which is often null).
    data_q = (
        select(Job)
        .where(and_(*filters))
        .order_by(
            desc(Job.first_seen_at),
            desc(Job.freshness_score),
            desc(Job.posted_at),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(data_q)
    jobs = list(result.scalars().all())

    # ── Background: refresh stale companies matching this search ─────────
    if page == 1:   # only on first page to avoid repeat triggers
        background_tasks.add_task(_trigger_stale_scans, q, db)

    return SearchResponse(
        jobs=[PublicJob.model_validate(j) for j in jobs],
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


# ── Single job detail ──────────────────────────────────────────────────────────

@router.get("/search/{job_id}", response_model=PublicJob)
async def get_job_public(job_id: int, db: AsyncSession = Depends(get_db)):
    """Public job detail — no login required."""
    result = await db.execute(
        select(Job).where(and_(Job.id == job_id, Job.active == True))
    )
    job = result.scalar_one_or_none()
    if not job:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job not found")
    return PublicJob.model_validate(job)


# ── Fresh jobs endpoint ────────────────────────────────────────────────────────

@router.get("/fresh", response_model=SearchResponse)
async def get_fresh_jobs(
    hours: int = Query(24, ge=1, le=168, description="Window in hours (1–168)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Jobs seen or posted within the last N hours, sorted by freshness_score DESC.
    No authentication required.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filters = [
        Job.active == True,
        Job.first_seen_at >= cutoff,
    ]

    count_q = select(func.count(Job.id)).where(and_(*filters))
    total = (await db.execute(count_q)).scalar() or 0

    data_q = (
        select(Job)
        .where(and_(*filters))
        .order_by(
            desc(Job.freshness_score),
            desc(Job.first_seen_at),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    jobs = list((await db.execute(data_q)).scalars().all())

    return SearchResponse(
        jobs=[PublicJob.model_validate(j) for j in jobs],
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


# ── Stats endpoint (for UI counters) ──────────────────────────────────────────

@router.get("/search/stats/summary")
async def search_stats(db: AsyncSession = Depends(get_db)):
    """Public stats for the homepage UI."""
    now = datetime.now(timezone.utc)

    total_q = await db.execute(
        select(func.count(Job.id)).where(Job.active == True)
    )
    last_24h_q = await db.execute(
        select(func.count(Job.id)).where(
            and_(Job.active == True, Job.first_seen_at >= now - timedelta(hours=24))
        )
    )
    return {
        "total_jobs": total_q.scalar() or 0,
        "last_24h": last_24h_q.scalar() or 0,
        "as_of": now.isoformat(),
    }
