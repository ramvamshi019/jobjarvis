"""ML pipeline Celery tasks.

Tasks:
  train_salary_model      — train GBM on jobs with salary data (weekly)
  predict_missing_salaries — infer salary_min/max for jobs missing it (daily)
  deduplicate_jobs         — mark near-duplicate job postings inactive (daily)
  detect_hiring_spikes     — flag companies with unusual hiring velocity (daily)
"""
import asyncio
import os
import pickle
import structlog
from app.workers.celery_app import celery_app
from app.database import async_engine

logger = structlog.get_logger(__name__)

MODEL_PATH = os.environ.get("ML_MODEL_PATH", "/app/data/models")
SALARY_MODEL_FILE = os.path.join(MODEL_PATH, "salary_model.pkl")
DEDUP_SIMILARITY_THRESHOLD = 0.92   # cosine similarity above this = duplicate
SPIKE_ZSCORE_THRESHOLD = 2.5        # z-score above this = hiring spike


def _run_async(coro):
    async def _w():
        await async_engine.dispose()
        return await coro
    return asyncio.run(_w())


# ── Celery task wrappers ──────────────────────────────────────────────────────

@celery_app.task(name="app.workers.ml_tasks.train_salary_model",
                 soft_time_limit=3600, max_retries=0)
def train_salary_model():
    """Train GBM salary predictor on jobs with known salary ranges."""
    return _run_async(_train_salary_model_async())


@celery_app.task(name="app.workers.ml_tasks.predict_missing_salaries",
                 soft_time_limit=1800, max_retries=1)
def predict_missing_salaries():
    """Run salary inference on jobs missing salary_min / salary_max."""
    return _run_async(_predict_salaries_async())


@celery_app.task(name="app.workers.ml_tasks.deduplicate_jobs",
                 soft_time_limit=3600, max_retries=1)
def deduplicate_jobs():
    """Detect and soft-delete near-duplicate job postings via embeddings."""
    return _run_async(_deduplicate_async())


@celery_app.task(name="app.workers.ml_tasks.detect_hiring_spikes",
                 soft_time_limit=600, max_retries=1)
def detect_hiring_spikes():
    """Flag companies whose daily posting count is a statistical outlier."""
    return _run_async(_detect_spikes_async())


# ── Salary model ──────────────────────────────────────────────────────────────

async def _train_salary_model_async() -> dict:
    """Train a GradientBoosting model to predict salary ranges."""
    from sqlalchemy import select, and_
    from app.database import AsyncSessionLocal
    from app.models.job import Job

    try:
        import numpy as np
        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.preprocessing import LabelEncoder
        from sklearn.pipeline import Pipeline
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.compose import ColumnTransformer
        from sklearn.preprocessing import OneHotEncoder
        from scipy.sparse import hstack
    except ImportError as e:
        logger.error("ml_tasks.salary.import_failed", error=str(e))
        return {"error": "scikit-learn not available"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Job).where(
                and_(
                    Job.active == True,
                    Job.salary_min != None,
                    Job.salary_max != None,
                    Job.salary_min > 0,
                    Job.salary_max > 0,
                    Job.salary_currency == "USD",
                )
            ).limit(20000)
        )
        jobs = list(result.scalars().all())

    if len(jobs) < 50:
        logger.warning("ml_tasks.salary.insufficient_data", count=len(jobs))
        return {"error": "insufficient_data", "count": len(jobs)}

    logger.info("ml_tasks.salary.training", samples=len(jobs))

    # ── Feature engineering ────────────────────────────────────────────────
    titles = [j.title or "" for j in jobs]
    locations = [j.country or "US" for j in jobs]
    exp_levels = [j.experience_level or "mid" for j in jobs]
    remote_types = [j.remote_type or "onsite" for j in jobs]
    skill_strs = [
        " ".join((j.required_skills or [])[:10]) for j in jobs
    ]

    # Target: predict log(salary) to reduce skew
    y_min = np.array([float(j.salary_min) for j in jobs])
    y_max = np.array([float(j.salary_max) for j in jobs])
    y = (y_min + y_max) / 2  # predict midpoint

    # TF-IDF on title + skills
    title_tfidf = TfidfVectorizer(max_features=200, ngram_range=(1, 2))
    skill_tfidf = TfidfVectorizer(max_features=100)
    X_title = title_tfidf.fit_transform(titles)
    X_skill = skill_tfidf.fit_transform(skill_strs)

    # Encode categoricals
    le_exp = LabelEncoder()
    le_remote = LabelEncoder()
    le_country = LabelEncoder()
    X_exp = le_exp.fit_transform(exp_levels).reshape(-1, 1)
    X_remote = le_remote.fit_transform(remote_types).reshape(-1, 1)
    X_country = le_country.fit_transform(locations).reshape(-1, 1)

    from scipy.sparse import csr_matrix
    X_cat = csr_matrix(np.hstack([X_exp, X_remote, X_country]))
    X = hstack([X_title, X_skill, X_cat])

    # Train
    model = GradientBoostingRegressor(
        n_estimators=100, max_depth=4, learning_rate=0.1,
        subsample=0.8, random_state=42
    )
    model.fit(X.toarray(), y)

    # Save model + encoders
    os.makedirs(MODEL_PATH, exist_ok=True)
    bundle = {
        "model": model,
        "title_tfidf": title_tfidf,
        "skill_tfidf": skill_tfidf,
        "le_exp": le_exp,
        "le_remote": le_remote,
        "le_country": le_country,
    }
    with open(SALARY_MODEL_FILE, "wb") as f:
        pickle.dump(bundle, f)

    logger.info("ml_tasks.salary.trained", samples=len(jobs), model_path=SALARY_MODEL_FILE)
    return {"status": "ok", "samples": len(jobs)}


async def _predict_salaries_async() -> dict:
    """Load trained model and predict salaries for jobs missing them."""
    if not os.path.exists(SALARY_MODEL_FILE):
        logger.warning("ml_tasks.salary.no_model")
        return {"status": "no_model"}

    try:
        import numpy as np
        from scipy.sparse import hstack, csr_matrix
    except ImportError:
        return {"error": "numpy/scipy not available"}

    with open(SALARY_MODEL_FILE, "rb") as f:
        bundle = pickle.load(f)

    model = bundle["model"]
    title_tfidf = bundle["title_tfidf"]
    skill_tfidf = bundle["skill_tfidf"]
    le_exp = bundle["le_exp"]
    le_remote = bundle["le_remote"]
    le_country = bundle["le_country"]

    from sqlalchemy import select, or_, and_
    from app.database import AsyncSessionLocal
    from app.models.job import Job

    BATCH = 500
    predicted = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Job).where(
                and_(
                    Job.active == True,
                    or_(Job.salary_min == None, Job.salary_max == None),
                    Job.title != None,
                )
            ).limit(BATCH)
        )
        jobs = list(result.scalars().all())

        if not jobs:
            return {"status": "ok", "predicted": 0}

        def _safe_encode(le, val, default="unknown"):
            try:
                return le.transform([val])[0]
            except ValueError:
                try:
                    return le.transform([default])[0]
                except ValueError:
                    return 0

        titles = [j.title or "" for j in jobs]
        exp_levels = [j.experience_level or "mid" for j in jobs]
        remote_types = [j.remote_type or "onsite" for j in jobs]
        locations = [j.country or "US" for j in jobs]
        skill_strs = [" ".join((j.required_skills or [])[:10]) for j in jobs]

        X_title = title_tfidf.transform(titles)
        X_skill = skill_tfidf.transform(skill_strs)
        X_exp = np.array([_safe_encode(le_exp, e, "mid") for e in exp_levels]).reshape(-1, 1)
        X_remote = np.array([_safe_encode(le_remote, r, "onsite") for r in remote_types]).reshape(-1, 1)
        X_country = np.array([_safe_encode(le_country, c, "US") for c in locations]).reshape(-1, 1)
        X_cat = csr_matrix(np.hstack([X_exp, X_remote, X_country]))
        X = hstack([X_title, X_skill, X_cat])

        preds = model.predict(X.toarray())

        for job, pred in zip(jobs, preds):
            mid = max(0, int(pred))
            if job.salary_min is None:
                job.salary_min = int(mid * 0.85)
            if job.salary_max is None:
                job.salary_max = int(mid * 1.15)
            if job.salary_currency is None:
                job.salary_currency = "USD"
            if job.salary_period is None:
                job.salary_period = "annual"
            predicted += 1

        await db.commit()

    logger.info("ml_tasks.salary.predicted", count=predicted)
    return {"status": "ok", "predicted": predicted}


# ── Deduplication ─────────────────────────────────────────────────────────────

async def _deduplicate_async() -> dict:
    """Find near-duplicate jobs (same company, high embedding cosine similarity)."""
    from sqlalchemy import select, and_
    from datetime import datetime, timezone, timedelta
    from app.database import AsyncSessionLocal
    from app.models.job import Job
    from app.models.ai_models import JobEmbedding

    duplicates_found = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    # Process per company — only compare within same company
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        # Get companies that have multiple recent active jobs with embeddings
        company_ids_q = await db.execute(
            select(Job.company_id)
            .join(JobEmbedding, JobEmbedding.job_id == Job.id)
            .where(and_(Job.active == True, Job.first_seen_at >= cutoff))
            .group_by(Job.company_id)
            .having(func.count(Job.id) > 1)
            .limit(200)
        )
        company_ids = [row[0] for row in company_ids_q.fetchall()]

    for company_id in company_ids:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Job.id, Job.title, JobEmbedding.embedding)
                .join(JobEmbedding, JobEmbedding.job_id == Job.id)
                .where(and_(
                    Job.company_id == company_id,
                    Job.active == True,
                    Job.first_seen_at >= cutoff,
                ))
                .order_by(Job.first_seen_at.desc())
            )
            rows = result.fetchall()

        if len(rows) < 2:
            continue

        # Pairwise similarity check
        dedup_ids: set[int] = set()
        for i, (id_i, title_i, emb_i) in enumerate(rows):
            if id_i in dedup_ids or emb_i is None:
                continue
            for id_j, title_j, emb_j in rows[i + 1:]:
                if id_j in dedup_ids or emb_j is None:
                    continue
                # Cosine similarity
                try:
                    if hasattr(emb_i, "tolist"):
                        vi, vj = emb_i.tolist(), emb_j.tolist()
                    else:
                        vi, vj = list(emb_i), list(emb_j)
                    from app.services.embedding_service import cosine_similarity
                    sim = cosine_similarity(vi, vj)
                    if sim >= DEDUP_SIMILARITY_THRESHOLD:
                        dedup_ids.add(id_j)  # keep id_i (newer), mark id_j as dup
                except Exception:
                    pass

        if dedup_ids:
            async with AsyncSessionLocal() as db:
                for dup_id in dedup_ids:
                    dup_job = await db.get(Job, dup_id)
                    if dup_job:
                        dup_job.active = False
                        dup_job.fingerprint = f"dup:{dup_id}"
                        duplicates_found += 1
                await db.commit()

    logger.info("ml_tasks.dedup_done", duplicates_marked=duplicates_found)
    return {"status": "ok", "duplicates_marked": duplicates_found}


# ── Hiring spike detection ────────────────────────────────────────────────────

async def _detect_spikes_async() -> dict:
    """Flag companies with statistically unusual daily job posting counts."""
    import math
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select, func, and_
    from app.database import AsyncSessionLocal
    from app.models.job import Job
    from app.models.ai_models import CompanyIntelligence
    from app.models.company import Company

    now = datetime.now(timezone.utc)
    spikes_found = 0

    async with AsyncSessionLocal() as db:
        # Get all active companies
        company_result = await db.execute(
            select(Company).where(Company.active == True).limit(5000)
        )
        companies = list(company_result.scalars().all())

    for company in companies:
        async with AsyncSessionLocal() as db:
            # Daily job counts over last 30 days
            daily_counts = []
            for days_ago in range(1, 31):
                day_start = now - timedelta(days=days_ago)
                day_end   = now - timedelta(days=days_ago - 1)
                count_q = await db.execute(
                    select(func.count(Job.id)).where(
                        and_(
                            Job.company_id == company.id,
                            Job.first_seen_at >= day_start,
                            Job.first_seen_at < day_end,
                        )
                    )
                )
                daily_counts.append(count_q.scalar() or 0)

            if len(daily_counts) < 7 or max(daily_counts) == 0:
                continue

            # Today's count
            today_count_q = await db.execute(
                select(func.count(Job.id)).where(
                    and_(
                        Job.company_id == company.id,
                        Job.first_seen_at >= now - timedelta(days=1),
                    )
                )
            )
            today_count = today_count_q.scalar() or 0

            if today_count == 0:
                continue

            # Z-score calculation
            n = len(daily_counts)
            mean = sum(daily_counts) / n
            variance = sum((x - mean) ** 2 for x in daily_counts) / n
            std = math.sqrt(variance) if variance > 0 else 0

            if std == 0:
                continue

            z_score = (today_count - mean) / std

            if z_score >= SPIKE_ZSCORE_THRESHOLD:
                # Update company intelligence with spike flag
                intel_q = await db.execute(
                    select(CompanyIntelligence).where(
                        CompanyIntelligence.company_id == company.id
                    )
                )
                intel = intel_q.scalar_one_or_none()
                if not intel:
                    intel = CompanyIntelligence(company_id=company.id)
                    db.add(intel)

                # Store spike info in hiring_signals JSON
                intel.hiring_velocity = float(today_count)
                if hasattr(intel, "hiring_signals"):
                    intel.hiring_signals = {
                        "spike_detected": True,
                        "z_score": round(z_score, 2),
                        "today_count": today_count,
                        "30d_mean": round(mean, 2),
                        "detected_at": now.isoformat(),
                    }
                spikes_found += 1
                await db.commit()
                logger.info("hiring_spike_detected",
                            company=company.name,
                            z_score=round(z_score, 2),
                            today=today_count,
                            mean=round(mean, 2))

    logger.info("ml_tasks.spike_detection_done", spikes=spikes_found)
    return {"status": "ok", "spikes_found": spikes_found}
