"""Celery scan tasks — ingestion pipeline."""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

from app.workers.celery_app import celery_app
from app.config import settings

logger = structlog.get_logger(__name__)


def _run_async(coro):
    """Run async coroutine from a synchronous Celery task.

    asyncio.run() creates a brand-new event loop each time it is called.
    The SQLAlchemy async engine holds connection-pool futures that are bound
    to whichever loop was current when the engine was first used.  When a
    Celery worker forks and then calls asyncio.run(), those old futures are
    attached to a *different* (now-dead) loop → RuntimeError.

    Fix: dispose the engine *inside* the new loop before touching the DB.
    dispose(close=True) drops all pooled connections so asyncpg will open
    fresh ones on the current loop.  It is cheap (no network round-trip) and
    idempotent, so calling it at the top of every task is safe.
    """
    async def _wrapper():
        from app.database import async_engine
        await async_engine.dispose()
        return await coro

    return asyncio.run(_wrapper())


@celery_app.task(bind=True, name="app.workers.scan_tasks.scan_tier_companies",
                 max_retries=2, soft_time_limit=3600)
def scan_tier_companies(self, tier: str = "tier1"):
    """Scan all due companies for a given tier."""
    return _run_async(_scan_tier_async(tier))


@celery_app.task(bind=True, name="app.workers.scan_tasks.run_company_scan_task",
                 max_retries=3, soft_time_limit=120)
def run_company_scan_task(self, company_id: int):
    """Scan a single company."""
    return _run_async(_scan_company_async(company_id))


@celery_app.task(bind=True, name="app.workers.scan_tasks.scan_new_companies",
                 max_retries=2, soft_time_limit=1800)
def scan_new_companies(self):
    """Immediately scan companies discovered in the last 2 hours.

    Runs every 30 min via beat so freshly-discovered companies get their
    jobs ingested within minutes rather than waiting for their tier window.
    """
    return _run_async(_scan_new_companies_async())


@celery_app.task(bind=True, name="app.workers.scan_tasks.promote_active_companies",
                 max_retries=2, soft_time_limit=300)
def promote_active_companies(self):
    """Auto-promote / demote companies based on recent hiring activity.

    Runs daily at 6 AM UTC.  Companies with lots of recent jobs move to a
    higher tier (higher priority_score); companies that haven't posted in a
    long time get demoted so we don't waste scan slots on them.
    """
    return _run_async(_promote_active_companies_async())


async def _scan_new_companies_async() -> dict:
    """Dispatch scan tasks for every company created in the last 2 hours."""
    from app.database import AsyncSessionLocal
    from app.models.company import Company
    from sqlalchemy import select, and_

    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

    async with AsyncSessionLocal() as db:
        q = select(Company).where(
            and_(
                Company.active == True,
                Company.is_blocklisted == False,
                Company.created_at >= cutoff,
                # Only companies that have ATS config set up
                Company.ats_type != None,
                Company.ats_identifier != None,
            )
        ).limit(500)
        result = await db.execute(q)
        companies = list(result.scalars().all())

    dispatched = 0
    for company in companies:
        run_company_scan_task.delay(company.id)
        dispatched += 1

    logger.info("scan_new_companies_dispatched", count=dispatched, cutoff_hours=2)
    return {"dispatched": dispatched}


async def _promote_active_companies_async() -> dict:
    """Update priority_score for all companies based on hiring activity."""
    from app.database import AsyncSessionLocal
    from app.models.company import Company
    from sqlalchemy import select, update

    now = datetime.now(timezone.utc)

    promoted = 0
    demoted = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Company).where(Company.active == True)
        )
        companies = list(result.scalars().all())

        for company in companies:
            old_score = company.priority_score

            # --- Promotion logic ---
            # Companies that posted a job recently → boost their score
            if company.last_job_found_at:
                days_since_job = (now - company.last_job_found_at).days
                if days_since_job <= 7 and company.jobs_found_count >= 20:
                    # Very active this week → tier1
                    company.priority_score = max(company.priority_score, 90)
                elif days_since_job <= 14 and company.jobs_found_count >= 5:
                    # Active past 2 weeks → at least tier2
                    company.priority_score = max(company.priority_score, 60)
                elif days_since_job <= 30:
                    # Posted recently → at least tier3
                    company.priority_score = max(company.priority_score, 20)
                else:
                    # No recent jobs (>30 days) → gently demote
                    company.priority_score = max(10, company.priority_score - 5)
            else:
                # Never found a job → low priority but keep alive for discovery
                if company.jobs_found_count == 0 and company.failure_count > 10:
                    company.priority_score = max(5, company.priority_score - 10)

            # High-volume companies always stay at tier1
            if company.jobs_found_count >= 100:
                company.priority_score = max(company.priority_score, 90)

            if company.priority_score > old_score:
                promoted += 1
            elif company.priority_score < old_score:
                demoted += 1

        await db.commit()

    logger.info("promote_active_companies_done", promoted=promoted, demoted=demoted,
                total=len(companies))
    return {"promoted": promoted, "demoted": demoted, "total": len(companies)}


async def _scan_tier_async(tier: str) -> dict:
    """Find and dispatch all due companies for the tier."""
    from app.database import AsyncSessionLocal
    from app.models.company import Company
    from sqlalchemy import select, and_

    now = datetime.now(timezone.utc)
    tier_filters = {
        "tier1": Company.priority_score >= 90,
        "tier2": and_(Company.priority_score >= 60, Company.priority_score < 90),
        "tier3": and_(Company.priority_score >= 20, Company.priority_score < 60),
    }

    async with AsyncSessionLocal() as db:
        q = select(Company).where(
            and_(
                Company.active == True,
                Company.is_blocklisted == False,
                tier_filters.get(tier, Company.priority_score >= 20),
                (Company.next_scan_at == None) | (Company.next_scan_at <= now),
            )
        ).limit(2000)
        result = await db.execute(q)
        companies = list(result.scalars().all())

    logger.info("tier_scan_start", tier=tier, count=len(companies))
    dispatched = 0
    for company in companies:
        run_company_scan_task.delay(company.id)
        dispatched += 1

    return {"tier": tier, "dispatched": dispatched}


async def _scan_company_async(company_id: int) -> dict:
    """Full ingestion pipeline for one company: Fetch → Bronze → Silver → Gold."""
    from app.database import AsyncSessionLocal
    from app.models.company import Company
    from app.models.ai_models import ScanRun, BronzeRawJob
    from app.connectors import get_connector, ATS_REGISTRY
    from app.services.normalizer import normalize_job, compute_data_quality_score
    from app.services.dedup import DedupEngine
    from app.services.freshness import compute_freshness
    from app.ai.role_classifier import classify_role
    from app.ai.skill_extractor import extract_skills
    from app.ai.spam_detector import detect_spam
    from app.ai.source_classifier import classify_source
    from app.ai.work_auth_detector import detect_work_auth
    from app.core.security import hash_content
    from app.services.compliance import log_fetch, check_robots_txt
    from sqlalchemy import select
    import tldextract

    start = time.monotonic()
    dedup = DedupEngine()

    async with AsyncSessionLocal() as db:
        # Load company
        result = await db.execute(select(Company).where(Company.id == company_id))
        company = result.scalar_one_or_none()
        if not company or not company.ats_type or not company.ats_identifier:
            return {"company_id": company_id, "skipped": True, "reason": "no_ats_config"}

        # Create scan run
        scan = ScanRun(company_id=company_id, scan_type="scheduled", status="running")
        db.add(scan)
        await db.flush()
        scan_id = scan.id

        domain = company.domain or tldextract.extract(company.career_url or "").registered_domain or ""

        # Compliance check
        allowed = await check_robots_txt(domain)
        if not allowed:
            scan.status = "blocked"
            scan.finished_at = datetime.now(timezone.utc)
            await db.commit()
            return {"company_id": company_id, "blocked": True}

        try:
            connector_cls = get_connector(company.ats_type)
            if not connector_cls:
                scan.status = "error"
                scan.error_message = f"Unknown ATS type: {company.ats_type}"
                scan.finished_at = datetime.now(timezone.utc)
                await db.commit()
                return {"company_id": company_id, "error": "unknown_ats"}

            async with connector_cls() as connector:
                conn_result = await connector.fetch_jobs(company_id, company.ats_identifier)

            # Log fetch
            await log_fetch(
                db, company_id, domain, company.career_url or "",
                status_code=200 if conn_result.success else 500,
                response_time_ms=conn_result.response_time_ms,
                success=conn_result.success,
                error_message=conn_result.error or "",
                jobs_found=len(conn_result.jobs),
            )

            if not conn_result.success:
                company.failure_count += 1
                company.consecutive_failures += 1
                company.last_checked_at = datetime.now(timezone.utc)
                # Exponential backoff for next scan
                backoff_mins = min(
                    settings.SCAN_MAX_RETRY_DELAY / 60,
                    company.scan_frequency_minutes * (2 ** min(company.consecutive_failures, 5))
                )
                company.next_scan_at = datetime.now(timezone.utc) + timedelta(minutes=backoff_mins)
                scan.status = "error"
                scan.error_message = conn_result.error
                scan.finished_at = datetime.now(timezone.utc)
                await db.commit()
                # ── Auto-heal: trigger healer after HEAL_THRESHOLD failures ──
                if company.consecutive_failures >= 3:
                    try:
                        from app.workers.healer_tasks import heal_single_company
                        heal_single_company.apply_async(
                            args=[company_id],
                            countdown=30,   # 30s delay — don't pile up
                        )
                        logger.info("healer_triggered", company=company.name,
                                    failures=company.consecutive_failures)
                    except Exception as he:
                        logger.warning("healer_trigger_failed", error=str(he))
                return {"company_id": company_id, "error": conn_result.error}

            # ── BRONZE LAYER ─────────────────────────────────────────────
            new_count = 0
            updated_count = 0

            # Per-batch in-memory fingerprint set for pre-insert dedup (Step 4)
            _batch_fps: set[str] = set()
            scan_invalid = 0

            for raw_job in conn_result.jobs:
                raw_hash = hash_content(str(raw_job.raw_json))

                # Bronze storage (always record raw, even if we skip Silver)
                bronze = BronzeRawJob(
                    scan_run_id=scan_id,
                    company_id=company_id,
                    source_type=company.ats_type,
                    raw_json=raw_job.raw_json,
                    external_id=raw_job.external_id,
                    raw_hash=raw_hash,
                    processed=False,
                )
                db.add(bronze)
                await db.flush()

                # ── SILVER LAYER: Normalize + Validate (Steps 1–3) ────────
                normalized = normalize_job(raw_job)
                if normalized is None:
                    # normalize_job already logged the reason (missing title/company/url)
                    scan_invalid += 1
                    continue

                # Step 2: fingerprint from normalize_job (title+company only)
                fp = normalized["fingerprint"]

                # Step 4: pre-insert dedup — skip if same fingerprint seen in batch
                if fp in _batch_fps:
                    logger.debug(
                        "scan_dedup_skip reason=batch_fingerprint company=%s title=%r",
                        company.name, normalized["title"],
                    )
                    continue
                _batch_fps.add(fp)

                # Role classify
                role_cls = classify_role(raw_job.title, raw_job.description or "")
                skills = extract_skills(raw_job.title, raw_job.description or "")
                spam = detect_spam({
                    "title": raw_job.title,
                    "description": raw_job.description or "",
                    "company_domain": company.domain,
                })
                source_cls = classify_source(
                    company.name, raw_job.description or "",
                    domain, company.ats_type
                )
                work_auth = detect_work_auth(raw_job.description or "", raw_job.title)
                freshness = compute_freshness(datetime.now(timezone.utc))

                # Step 9: compute and store data quality score
                dqs = compute_data_quality_score(
                    normalized["description"],
                    skills.required_skills,
                    normalized["country"],
                    role_cls.role_category,
                )

                job_data = {
                    "company_id":          company_id,
                    "external_id":         raw_job.external_id,
                    "title":               normalized["title"],
                    "company_name":        normalized["company"],
                    "location":            normalized["location"],
                    "city":                normalized.get("city"),
                    "region":              normalized.get("region"),
                    "job_url":             normalized["job_url"],
                    "apply_url":           raw_job.apply_url or normalized["job_url"],
                    "description":         normalized["description"],
                    "description_html":    normalized["description_html"],
                    "employment_type":     normalized["employment_type"],
                    "experience_level":    normalized["experience_level"],
                    "role_category":       role_cls.role_category,
                    "role_confidence":     role_cls.confidence_score,
                    "salary_min":          normalized.get("salary_min"),
                    "salary_max":          normalized.get("salary_max"),
                    "salary_currency":     normalized.get("salary_currency", "USD"),
                    "salary_period":       normalized.get("salary_period"),
                    "required_skills":     skills.required_skills,
                    "preferred_skills":    skills.preferred_skills,
                    "matched_tools":       skills.all_skills,
                    "spam_score":          spam.spam_score,
                    "spam_flags_json":     {"flags": spam.spam_flags},
                    "work_auth_flags_json":{"flags": work_auth.work_auth_flags},
                    "eligibility_risk_score": work_auth.eligibility_risk_score,
                    # Step 3: use classify_source value, already canonical
                    "source_type":         source_cls.source_type,
                    "source_confidence":   source_cls.confidence,
                    "fingerprint":         fp,
                    "raw_hash":            raw_hash,
                    "normalized_title":    normalized["normalized_title"],
                    "normalized_location": normalized["normalized_location"],
                    "country":             normalized["country"],
                    "remote_type":         normalized["remote_type"],
                    "freshness_label":     freshness,
                    "posted_at":           raw_job.posted_at,
                    "active":              True,
                    "source":              company.ats_type,
                    "data_quality_score":  dqs,
                }

                job_obj, is_new = await dedup.upsert_job(db, job_data)
                bronze.silver_job_id = job_obj.id
                bronze.processed = True

                if is_new:
                    new_count += 1
                else:
                    updated_count += 1

            # Update company scan metadata
            company.last_checked_at = datetime.now(timezone.utc)
            company.last_success_at = datetime.now(timezone.utc)
            company.consecutive_failures = 0
            company.next_scan_at = datetime.now(timezone.utc) + timedelta(
                minutes=company.scan_frequency_minutes
            )

            scan.status = "completed"
            scan.jobs_fetched = len(conn_result.jobs)
            scan.jobs_new = new_count
            scan.jobs_updated = updated_count
            scan.duration_seconds = time.monotonic() - start
            scan.finished_at = datetime.now(timezone.utc)

            await db.commit()

            # Step 10: per-run summary line
            logger.info(
                "scan_complete company=%s ats=%s "
                "checked=%d | inserted=%d | duplicates=%d | invalid=%d | duration=%.2fs",
                company.name, company.ats_type,
                len(conn_result.jobs),
                new_count,
                len(_batch_fps) - new_count,
                scan_invalid,
                time.monotonic() - start,
            )
            return {
                "company_id":    company_id,
                "company_name":  company.name,
                "jobs_fetched":  len(conn_result.jobs),
                "jobs_new":      new_count,
                "jobs_updated":  updated_count,
                "jobs_invalid":  scan_invalid,
                "duration_s":    round(time.monotonic() - start, 2),
            }

        except Exception as e:
            logger.error("scan_error", company_id=company_id, error=str(e))
            try:
                scan.status = "error"
                scan.error_message = str(e)[:500]
                scan.finished_at = datetime.now(timezone.utc)
                company.failure_count += 1
                company.consecutive_failures += 1
                await db.commit()
            except Exception:
                pass
            raise
