"""Celery tasks for generating and storing job/resume embeddings.

Tasks:
  embed_new_jobs     — runs hourly, embeds jobs that have no embedding yet
  embed_all_jobs     — one-shot backfill for all jobs
  embed_single_job   — on-demand embedding for one job (called post-ingest)
  embed_resume       — embed a specific resume version

The embedding model (sentence-transformers all-MiniLM-L6-v2) is loaded once
per worker process and cached via functools.lru_cache.
"""
import asyncio
import structlog
from app.workers.celery_app import celery_app
from app.database import async_engine

logger = structlog.get_logger(__name__)

BATCH_SIZE = 200   # jobs per embedding batch


def _run_async(coro):
    async def _w():
        await async_engine.dispose()
        return await coro
    return asyncio.run(_w())


# ── Celery task definitions ───────────────────────────────────────────────────

@celery_app.task(name="app.workers.embedding_tasks.embed_new_jobs",
                 soft_time_limit=3600, max_retries=1)
def embed_new_jobs():
    """
    Embed jobs that don't have an embedding yet (runs every 15 min via beat).

    Uses subprocess to invoke the standalone backfill script — bypasses the
    fork+asyncpg deadlock that the SQLAlchemy session approach hits inside
    Celery's prefork worker.
    """
    import subprocess
    import os
    script = "/tmp/backfill_embeddings.py"
    if not os.path.exists(script):
        # Fallback: copy from mounted /app/scripts if /tmp version is missing
        alt = "/app/scripts/backfill_embeddings.py"
        if os.path.exists(alt):
            script = alt
        else:
            return {"error": "backfill_embeddings.py not found"}
    try:
        # Cap each run at 500 jobs so the beat schedule stays responsive.
        # With ~30 jobs/sec that's ~17 sec per run, well under the 15-min interval.
        result = subprocess.run(
            ["python3", "-u", script, "--limit", "500"],
            capture_output=True, text=True, timeout=3000,
        )
        return {
            "status": "ok" if result.returncode == 0 else "failed",
            "stdout_tail": result.stdout[-500:],
            "stderr_tail": result.stderr[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@celery_app.task(name="app.workers.embedding_tasks.embed_all_jobs",
                 soft_time_limit=7200, max_retries=0)
def embed_all_jobs():
    """Full backfill — embed every active job (run once manually)."""
    return _run_async(_embed_all_jobs_async())


@celery_app.task(name="app.workers.embedding_tasks.embed_single_job",
                 soft_time_limit=60, max_retries=2)
def embed_single_job(job_id: int):
    """Embed a single job by ID (called post-ingest)."""
    return _run_async(_embed_single_job_async(job_id))


@celery_app.task(name="app.workers.embedding_tasks.embed_resume",
                 soft_time_limit=120, max_retries=2)
def embed_resume(resume_version_id: int):
    """Embed a specific resume version."""
    return _run_async(_embed_resume_async(resume_version_id))


# ── Async implementations ─────────────────────────────────────────────────────

async def _embed_new_jobs_async() -> dict:
    from sqlalchemy import select, not_, exists
    from app.database import AsyncSessionLocal
    from app.models.job import Job
    from app.models.ai_models import JobEmbedding
    from app.services.embedding_service import generate_embedding, generate_job_embedding_text

    embedded = 0
    async with AsyncSessionLocal() as db:
        # Jobs without an embedding entry
        q = (
            select(Job)
            .where(
                Job.active == True,
                not_(exists().where(JobEmbedding.job_id == Job.id))
            )
            .order_by(Job.first_seen_at.desc())
            .limit(BATCH_SIZE)
        )
        result = await db.execute(q)
        jobs = list(result.scalars().all())

        for job in jobs:
            try:
                text = generate_job_embedding_text(
                    title=job.title or "",
                    description=job.description or "",
                    skills=(job.required_skills or []) + (job.preferred_skills or []),
                    location=job.location or "",
                )
                vec = generate_embedding(text)
                emb = JobEmbedding(
                    job_id=job.id,
                    model="all-MiniLM-L6-v2",
                    embedding=vec,
                )
                db.add(emb)
                embedded += 1
            except Exception as e:
                logger.warning("embed_job_failed", job_id=job.id, error=str(e))

        if embedded:
            await db.commit()

    logger.info("embed_new_jobs_done", embedded=embedded)
    return {"embedded": embedded}


async def _embed_all_jobs_async() -> dict:
    """Backfill embeddings for ALL jobs in batches."""
    from sqlalchemy import select, not_, exists, func
    from app.database import AsyncSessionLocal
    from app.models.job import Job
    from app.models.ai_models import JobEmbedding
    from app.services.embedding_service import generate_embedding, generate_job_embedding_text

    total_embedded = 0
    offset = 0

    while True:
        async with AsyncSessionLocal() as db:
            q = (
                select(Job)
                .where(
                    Job.active == True,
                    not_(exists().where(JobEmbedding.job_id == Job.id))
                )
                .order_by(Job.id)
                .offset(offset)
                .limit(BATCH_SIZE)
            )
            result = await db.execute(q)
            jobs = list(result.scalars().all())

            if not jobs:
                break

            batch_embedded = 0
            for job in jobs:
                try:
                    text = generate_job_embedding_text(
                        title=job.title or "",
                        description=job.description or "",
                        skills=(job.required_skills or []) + (job.preferred_skills or []),
                        location=job.location or "",
                    )
                    vec = generate_embedding(text)
                    db.add(JobEmbedding(
                        job_id=job.id,
                        model="all-MiniLM-L6-v2",
                        embedding=vec,
                    ))
                    batch_embedded += 1
                except Exception as e:
                    logger.warning("embed_job_failed", job_id=job.id, error=str(e))

            if batch_embedded:
                await db.commit()

            total_embedded += batch_embedded
            offset += BATCH_SIZE
            logger.info("embed_batch_done", offset=offset, total=total_embedded)

    logger.info("embed_all_jobs_done", total=total_embedded)
    return {"total_embedded": total_embedded}


async def _embed_single_job_async(job_id: int) -> dict:
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.job import Job
    from app.models.ai_models import JobEmbedding
    from app.services.embedding_service import generate_embedding, generate_job_embedding_text

    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        if not job:
            return {"error": f"Job {job_id} not found"}

        # Check if already embedded
        existing = await db.execute(
            select(JobEmbedding).where(JobEmbedding.job_id == job_id)
        )
        if existing.scalar_one_or_none():
            return {"status": "already_embedded", "job_id": job_id}

        text = generate_job_embedding_text(
            title=job.title or "",
            description=job.description or "",
            skills=(job.required_skills or []) + (job.preferred_skills or []),
            location=job.location or "",
        )
        vec = generate_embedding(text)
        db.add(JobEmbedding(
            job_id=job.id,
            model="all-MiniLM-L6-v2",
            embedding=vec,
        ))
        await db.commit()

    return {"status": "ok", "job_id": job_id}


async def _embed_resume_async(resume_version_id: int) -> dict:
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.ai_models import ResumeEmbedding
    from app.services.embedding_service import generate_embedding

    async with AsyncSessionLocal() as db:
        # Try to load resume version — import here to avoid circular
        try:
            from app.models.resume import ResumeVersion
            rv = await db.get(ResumeVersion, resume_version_id)
            if not rv:
                return {"error": f"ResumeVersion {resume_version_id} not found"}
            # Build text from resume content
            content = getattr(rv, "content_text", "") or getattr(rv, "parsed_json", {})
            if isinstance(content, dict):
                skills = content.get("skills", [])
                summary = content.get("summary", "")
                experience = " ".join(
                    str(e.get("description", "")) for e in content.get("experience", [])
                )
                text = f"Skills: {', '.join(skills)}\n{summary}\n{experience}"
            else:
                text = str(content)[:3000]
        except Exception as e:
            logger.error("embed_resume_load_failed", error=str(e))
            return {"error": str(e)}

        existing = await db.execute(
            select(ResumeEmbedding).where(ResumeEmbedding.resume_id == resume_version_id)
        )
        if existing.scalar_one_or_none():
            return {"status": "already_embedded", "resume_version_id": resume_version_id}

        vec = generate_embedding(text)
        db.add(ResumeEmbedding(
            resume_id=resume_version_id,
            model="all-MiniLM-L6-v2",
            embedding=vec,
        ))
        await db.commit()

    return {"status": "ok", "resume_version_id": resume_version_id}
