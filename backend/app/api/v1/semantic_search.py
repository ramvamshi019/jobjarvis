"""Semantic (vector) search API.

Endpoints:
  GET  /jobs/semantic?q=...          — vector similarity search
  GET  /jobs/similar/{job_id}        — find jobs similar to a given job
  POST /jobs/resume-match            — rank jobs against a resume text

Uses pgvector cosine distance (<=>).  Falls back gracefully to keyword search
if no embeddings exist yet (e.g. shortly after deploy).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.ai_models import JobEmbedding
from app.services.embedding_service import (
    generate_embedding,
    generate_job_embedding_text,
)

router = APIRouter(prefix="/jobs", tags=["semantic-search"])

# ── Response schemas ───────────────────────────────────────────────────────────

class SemanticJob(BaseModel):
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
    job_url: Optional[str]
    required_skills: Optional[list]
    similarity_score: float

    model_config = {"from_attributes": True}


class SemanticSearchResponse(BaseModel):
    jobs: list[SemanticJob]
    total: int
    query: str
    search_type: str   # "semantic" | "keyword_fallback"


class ResumeMatchRequest(BaseModel):
    resume_text: str
    top_k: int = 20
    min_similarity: float = 0.3


# ── Semantic search endpoint ──────────────────────────────────────────────────

@router.get("/semantic", response_model=SemanticSearchResponse)
async def semantic_search(
    q: str = Query(..., min_length=2, description="Natural language job query"),
    top_k: int = Query(25, ge=1, le=100),
    min_similarity: float = Query(0.25, ge=0.0, le=1.0),
    experience: Optional[str] = Query(None),
    remote: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Semantic job search using vector cosine similarity.

    Example: q="build data pipelines with Spark and Airflow"
    Returns jobs ranked by semantic relevance, not just keyword match.
    """
    # Generate query embedding
    query_vec = generate_embedding(q)
    is_zero = all(v == 0.0 for v in query_vec)

    if is_zero:
        # Fall back to keyword search
        return await _keyword_fallback(q, top_k, db)

    # Build optional filters as SQL conditions
    extra_conditions = ["j.active = true"]
    if experience:
        extra_conditions.append(f"j.experience_level = '{experience.lower()}'")
    if remote:
        extra_conditions.append(f"j.remote_type = '{remote.lower()}'")
    if country:
        extra_conditions.append(f"j.country = '{country.upper()}'")

    where_clause = " AND ".join(extra_conditions)

    # pgvector cosine distance — lower = more similar
    # 1 - cosine_distance = cosine_similarity
    sql = text(f"""
        SELECT
            j.id,
            j.title,
            j.company_name,
            j.location,
            j.remote_type,
            j.experience_level,
            j.employment_type,
            j.role_category,
            j.salary_min,
            j.salary_max,
            j.url          AS job_url,
            j.required_skills,
            (1 - (e.embedding <=> CAST(:vec AS vector))) AS similarity_score
        FROM job_embeddings e
        JOIN jobs j ON j.id = e.job_id
        WHERE {where_clause}
          AND (1 - (e.embedding <=> CAST(:vec AS vector))) >= :min_sim
        ORDER BY e.embedding <=> CAST(:vec AS vector)
        LIMIT :top_k
    """)

    try:
        result = await db.execute(sql, {
            "vec": str(query_vec),
            "min_sim": min_similarity,
            "top_k": top_k,
        })
        rows = result.mappings().all()
    except Exception:
        # pgvector not available or no embeddings — fall back
        return await _keyword_fallback(q, top_k, db)

    jobs = [
        SemanticJob(
            id=row["id"],
            title=row["title"],
            company_name=row["company_name"],
            location=row["location"],
            remote_type=row["remote_type"],
            experience_level=row["experience_level"],
            employment_type=row["employment_type"],
            role_category=row["role_category"],
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            job_url=row["job_url"],
            required_skills=row["required_skills"],
            similarity_score=round(float(row["similarity_score"]), 4),
        )
        for row in rows
    ]

    return SemanticSearchResponse(
        jobs=jobs,
        total=len(jobs),
        query=q,
        search_type="semantic",
    )


# ── Similar jobs endpoint ─────────────────────────────────────────────────────

@router.get("/similar/{job_id}", response_model=SemanticSearchResponse)
async def similar_jobs(
    job_id: int,
    top_k: int = Query(10, ge=1, le=50),
    min_similarity: float = Query(0.5, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
):
    """Find jobs semantically similar to a given job."""
    # Get this job's embedding
    emb_q = await db.execute(
        select(JobEmbedding).where(JobEmbedding.job_id == job_id)
    )
    emb = emb_q.scalar_one_or_none()
    if not emb or emb.embedding is None:
        raise HTTPException(status_code=404, detail="No embedding found for this job")

    job = await db.get(Job, job_id)
    query_label = job.title if job else str(job_id)

    sql = text("""
        SELECT
            j.id, j.title, j.company_name, j.location, j.remote_type,
            j.experience_level, j.employment_type, j.role_category,
            j.salary_min, j.salary_max, j.url AS job_url, j.required_skills,
            (1 - (e.embedding <=> CAST(:vec AS vector))) AS similarity_score
        FROM job_embeddings e
        JOIN jobs j ON j.id = e.job_id
        WHERE j.active = true
          AND j.id != :exclude_id
          AND (1 - (e.embedding <=> CAST(:vec AS vector))) >= :min_sim
        ORDER BY e.embedding <=> CAST(:vec AS vector)
        LIMIT :top_k
    """)

    try:
        result = await db.execute(sql, {
            "vec": str(emb.embedding),
            "exclude_id": job_id,
            "min_sim": min_similarity,
            "top_k": top_k,
        })
        rows = result.mappings().all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Vector search error: {e}")

    jobs = [
        SemanticJob(
            id=row["id"],
            title=row["title"],
            company_name=row["company_name"],
            location=row["location"],
            remote_type=row["remote_type"],
            experience_level=row["experience_level"],
            employment_type=row["employment_type"],
            role_category=row["role_category"],
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            job_url=row["job_url"],
            required_skills=row["required_skills"],
            similarity_score=round(float(row["similarity_score"]), 4),
        )
        for row in rows
    ]

    return SemanticSearchResponse(
        jobs=jobs,
        total=len(jobs),
        query=f"Similar to: {query_label}",
        search_type="semantic",
    )


# ── Resume-to-job matching endpoint ──────────────────────────────────────────

@router.post("/resume-match", response_model=SemanticSearchResponse)
async def match_resume_to_jobs(
    body: ResumeMatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Given raw resume text, find the most relevant job postings.

    Embed the resume → cosine similarity against all job embeddings →
    return ranked matches.  Also returns skill gap analysis per job.
    """
    resume_vec = generate_embedding(body.resume_text[:3000])
    if all(v == 0.0 for v in resume_vec):
        raise HTTPException(status_code=503, detail="Embedding model not available")

    sql = text("""
        SELECT
            j.id, j.title, j.company_name, j.location, j.remote_type,
            j.experience_level, j.employment_type, j.role_category,
            j.salary_min, j.salary_max, j.url AS job_url, j.required_skills,
            (1 - (e.embedding <=> CAST(:vec AS vector))) AS similarity_score
        FROM job_embeddings e
        JOIN jobs j ON j.id = e.job_id
        WHERE j.active = true
          AND (1 - (e.embedding <=> CAST(:vec AS vector))) >= :min_sim
        ORDER BY e.embedding <=> CAST(:vec AS vector)
        LIMIT :top_k
    """)

    try:
        result = await db.execute(sql, {
            "vec": str(resume_vec),
            "min_sim": body.min_similarity,
            "top_k": body.top_k,
        })
        rows = result.mappings().all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Vector search error: {e}")

    jobs = [
        SemanticJob(
            id=row["id"],
            title=row["title"],
            company_name=row["company_name"],
            location=row["location"],
            remote_type=row["remote_type"],
            experience_level=row["experience_level"],
            employment_type=row["employment_type"],
            role_category=row["role_category"],
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            job_url=row["job_url"],
            required_skills=row["required_skills"],
            similarity_score=round(float(row["similarity_score"]), 4),
        )
        for row in rows
    ]

    return SemanticSearchResponse(
        jobs=jobs,
        total=len(jobs),
        query="resume_match",
        search_type="semantic",
    )


# ── Keyword fallback ──────────────────────────────────────────────────────────

async def _keyword_fallback(q: str, top_k: int, db: AsyncSession) -> SemanticSearchResponse:
    """ILIKE keyword fallback when embeddings aren't available."""
    kw = f"%{q.strip()}%"
    result = await db.execute(
        select(Job)
        .where(
            and_(
                Job.active == True,
                Job.title.ilike(kw),
            )
        )
        .limit(top_k)
    )
    jobs_orm = list(result.scalars().all())
    jobs = [
        SemanticJob(
            id=j.id,
            title=j.title,
            company_name=j.company_name,
            location=j.location,
            remote_type=j.remote_type,
            experience_level=j.experience_level,
            employment_type=j.employment_type,
            role_category=j.role_category,
            salary_min=j.salary_min,
            salary_max=j.salary_max,
            job_url=j.job_url,
            required_skills=j.required_skills,
            similarity_score=1.0,
        )
        for j in jobs_orm
    ]
    return SemanticSearchResponse(
        jobs=jobs, total=len(jobs), query=q, search_type="keyword_fallback"
    )
