"""Celery scan tasks — ingestion pipeline."""
import asyncio
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

from app.workers.celery_app import celery_app
from app.config import settings

logger = structlog.get_logger(__name__)

# Conservative tech-only ingestion gate. We only drop a job when its title
# clearly names a non-tech occupation AND carries no engineering/IT signal.
# Anything with a tech signal (e.g. "Sales Engineer", "Security Engineer")
# is always kept, so this never costs us real tech jobs — it just stops
# whole Workday tenants (hospitals, retail, food service) from flooding the
# corpus with cooks/nurses/cashiers.
_NON_TECH_RE = re.compile(
    r'\b(registered\s+nurse|nurse|\brn\b|\blpn\b|\bcna\b|physician|surgeon|'
    r'medical\s+assistant|caregiver|phlebotom\w*|radiolog\w*|sonograph\w*|'
    r'hygienist|pharmacist|dental|veterinar\w*|paramedic|therapist|counselor|'
    r'cook|chef|barista|server|waiter|waitress|bartender|dishwasher|'
    r'food\s+service|line\s+cook|prep\s+cook|housekeep\w*|janitor|custodian|'
    r'groundskeep\w*|landscap\w*|cashier|teller|stocker|store\s+associate|'
    r'retail\s+associate|sales\s+associate|warehouse\s+associate|forklift|'
    r'\bdriver\b|\bcdl\b|courier|delivery|security\s+guard|\bguard\b|'
    r'plumber|electrician|\bhvac\b|welder|machinist|assembler|laborer|'
    r'teacher|tutor|professor|faculty|social\s+worker|firefighter|'
    r'receptionist|secretary|bookkeeper|loan\s+officer|insurance\s+agent|'
    r'real\s+estate)\b',
    re.IGNORECASE,
)
_TECH_SIGNAL_RE = re.compile(
    r'\b(engineer|engineering|developer|software|programmer|\bdata\b|devops|'
    r'\bsre\b|site\s+reliability|analyst|scientist|architect|\bqa\b|sdet|'
    r'cloud|infrastructure|platform|database|\bdba\b|machine\s+learning|'
    r'\bml\b|\bai\b|cyber|information\s+technology|technical|backend|'
    r'frontend|front[\s-]end|back[\s-]end|full[\s-]?stack)\b',
    re.IGNORECASE,
)


def _looks_non_tech(title: str) -> bool:
    if not title:
        return False
    if _TECH_SIGNAL_RE.search(title):
        return False
    return bool(_NON_TECH_RE.search(title))


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
    """Re-prioritise companies by *tech-job* volume.

    The old logic ranked purely on raw job count, so a hospital Workday
    tenant posting 500 nurse roles rode tier1 (scanned every 10 min) while a
    lean startup posting 5 engineering roles starved in tier3/tier4. We now
    score on how many *tech* jobs (role_category in RELEVANT_ROLES) a company
    produced recently, cap high-volume non-tech employers out of the fast
    tiers, and protect never-scanned companies so new tech companies get a
    fair chance before they can decay into the un-scanned pool.
    """
    from app.database import AsyncSessionLocal
    from app.models.company import Company
    from app.models.job import Job
    from app.ai.role_classifier import RELEVANT_ROLES
    from sqlalchemy import select, func, and_

    now = datetime.now(timezone.utc)
    cutoff30 = now - timedelta(days=30)
    cutoff14 = now - timedelta(days=14)
    tech_roles = list(RELEVANT_ROLES)

    promoted = demoted = capped = protected = 0

    async with AsyncSessionLocal() as db:
        # One grouped pass: tech & total job counts per company (last 30d).
        agg = await db.execute(
            select(
                Job.company_id,
                func.count().filter(Job.role_category.in_(tech_roles)).label("tech30"),
                func.count()
                    .filter(
                        and_(
                            Job.role_category.in_(tech_roles),
                            Job.first_seen_at >= cutoff14,
                        )
                    ).label("tech14"),
                func.count().label("all30"),
            )
            .where(and_(Job.active == True, Job.first_seen_at >= cutoff30))
            .group_by(Job.company_id)
        )
        stats = {
            r.company_id: (r.tech30, r.tech14, r.all30) for r in agg.all()
        }

        result = await db.execute(select(Company).where(Company.active == True))
        companies = list(result.scalars().all())

        for company in companies:
            old_score = company.priority_score
            tech30, tech14, all30 = stats.get(company.id, (0, 0, 0))
            never_scanned = company.last_success_at is None

            if tech14 >= 10 or tech30 >= 100:
                # Pumping out tech roles → fastest tier.
                company.priority_score = max(company.priority_score, 90)
            elif tech30 >= 10:
                company.priority_score = max(company.priority_score, 60)
            elif tech30 >= 1:
                # Any tech presence → keep it scanned frequently (tier3).
                company.priority_score = max(company.priority_score, 35)
            elif tech30 == 0 and all30 >= 50:
                # High-volume but zero tech (hospital/retail tenant) → evict
                # from the fast tiers so it stops hogging the scan budget,
                # but keep it scannable in case it ever posts engineering.
                company.priority_score = min(company.priority_score, 30)
                capped += 1
            elif never_scanned:
                # Brand-new / never successfully scanned → protect: keep it
                # in a scannable tier so tier4-rescue + scan-new can prove it
                # before it can be demoted away.
                company.priority_score = max(company.priority_score, 25)
                protected += 1
            else:
                # Genuinely dead (scanned, no tech, low volume) → decay, but
                # never to 0 so tier4-rescue can re-check it occasionally.
                company.priority_score = max(5, company.priority_score - 10)

            if company.priority_score > old_score:
                promoted += 1
            elif company.priority_score < old_score:
                demoted += 1

        await db.commit()

    logger.info(
        "promote_active_companies_done",
        promoted=promoted, demoted=demoted, capped=capped,
        protected=protected, total=len(companies),
    )
    return {
        "promoted": promoted, "demoted": demoted, "capped": capped,
        "protected": protected, "total": len(companies),
    }


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
        "tier4": Company.priority_score < 20,
    }
    # tier3/tier4 hold the long tail — give them a much bigger drain budget
    # so the backlog of never-scanned (often tech) companies actually clears.
    tier_limits = {"tier1": 2000, "tier2": 2000, "tier3": 5000, "tier4": 5000}

    async with AsyncSessionLocal() as db:
        q = (
            select(Company)
            .where(
                and_(
                    Company.active == True,
                    Company.is_blocklisted == False,
                    tier_filters.get(tier, Company.priority_score >= 20),
                    (Company.next_scan_at == None) | (Company.next_scan_at <= now),
                )
            )
            # Oldest-due first, and NULL next_scan_at (never scanned) first of
            # all — without this ORDER BY Postgres returned an arbitrary slice
            # every run, so the same companies were re-scanned while the rest
            # starved forever.
            .order_by(
                Company.next_scan_at.asc().nulls_first(),
                Company.priority_score.desc(),
            )
            .limit(tier_limits.get(tier, 2000))
        )
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

                # Tech-only gate: drop clearly non-tech roles so big Workday
                # tenants don't dilute the corpus with cooks/nurses/cashiers.
                if _looks_non_tech(normalized["title"]):
                    logger.debug(
                        "scan_skip reason=non_tech company=%s title=%r",
                        company.name, normalized["title"],
                    )
                    continue

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
                # Base freshness on the real posting date when the source
                # provides one; fall back to scrape time only when unknown.
                # Otherwise a months-old listing we just discovered would be
                # mislabeled "new".
                freshness = compute_freshness(
                    raw_job.posted_at or datetime.now(timezone.utc)
                )

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
