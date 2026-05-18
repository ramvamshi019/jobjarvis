"""Celery scan tasks — ingestion pipeline."""
import asyncio
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

from app.workers.celery_app import celery_app
from app.config import settings

logger = structlog.get_logger(__name__)

# ── Tech-only ingestion filter ─────────────────────────────────────────────────
# When INGEST_TECH_ONLY=1 (default), we skip jobs whose normalized title is
# clearly non-engineering (sales, marketing, HR, retail, hospitality, healthcare,
# operations, finance, etc.).  This is the single biggest lever for cleaning up
# the corpus when we're probing enterprises like SmartRecruiters customers that
# post thousands of cashier/nurse/warehouse jobs.
#
# Two-stage filter:
#   1.  TECH_ALLOW_RE matches → accept (covers most engineering roles and
#       "Sales Engineer" / "Solutions Engineer" / "Customer Success Engineer"
#       which are arguably tech-adjacent and worth keeping)
#   2.  NON_TECH_REJECT_RE matches → reject
#   3.  otherwise → accept (be permissive on ambiguous titles, the downstream
#       role_category will tag them but they won't get blocked)
#
# Disable per-deployment with `INGEST_TECH_ONLY=0` in the env.
INGEST_TECH_ONLY: bool = os.getenv("INGEST_TECH_ONLY", "1").lower() in ("1", "true", "yes")

_TECH_ALLOW_RE = re.compile(
    r"\b("
    r"engineer|developer|programmer|architect|sre|devops|dev[\s-]?ops"
    r"|data\s+scientist|data\s+analyst|machine\s+learning|deep\s+learning|llm|genai"
    r"|software|backend|back[\s-]end|frontend|front[\s-]end|full[\s-]?stack"
    r"|api|platform|infrastructure|cloud|kubernetes|sysadmin|security\s+engineer"
    r"|qa|sdet|test\s+engineer|automation\s+engineer"
    r"|technical\s+(lead|writer|program\s+manager)|cto|vp\s+engineering"
    r"|research\s+(scientist|engineer)|robotics"
    r")\b",
    re.I,
)

_NON_TECH_REJECT_RE = re.compile(
    r"\b("
    # Retail / hospitality / food / cleaning
    r"cashier|barista|server|waiter|waitress|bartender|host|hostess|busser"
    r"|housekeeper|janitor|cleaner|custodian|maintenance\s+(tech|worker)"
    r"|stock(er|ing)?|stocking|warehouse\s+associate|warehouse\s+worker|picker|packer|forklift"
    r"|retail\s+(associate|sales|clerk|cashier)|sales\s+associate|store\s+(manager|associate)"
    r"|line\s+cook|prep\s+cook|chef|baker|butcher|deli|pizza|crew\s+member"
    r"|valet|doorman|porter|attendant|concierge|bellman|bellhop"
    # Driving / logistics labour
    r"|driver|chauffeur|courier|delivery\s+driver|truck\s+driver|cdl"
    # Healthcare clinical
    r"|nurse|nursing|rn\b|lpn\b|cna\b|caregiver|caretaker|aide|orderly"
    r"|physician|doctor|dentist|surgeon|pharmacist|therapist|sonographer"
    r"|radiologist|paramedic|emt|phlebotomist|medical\s+(assistant|technologist)"
    # Trades / construction / labour
    r"|plumber|electrician|carpenter|welder|mechanic|technician\s+(trainee|apprentice)"
    r"|labourer|laborer|construction|roofer|mason|painter|installer"
    # HR / talent / recruiting (almost never engineering)
    r"|recruiter|talent\s+(acquisition|partner|sourcer)|sourcer\b|hr\s+(generalist|specialist|coordinator|business\s+partner)"
    # Sales (keep "sales engineer" / "solutions engineer" via allow list)
    r"|account\s+(executive|manager)|sdr\b|bdr\b|business\s+development\s+rep"
    r"|inside\s+sales|outside\s+sales|sales\s+(rep|representative|associate|director|manager|coordinator)"
    # Marketing / brand / content / design (non-engineering)
    r"|marketing\s+(manager|specialist|coordinator|director|associate|analyst)"
    r"|brand\s+(manager|specialist|director|ambassador)|copywriter|content\s+(writer|creator|strategist)"
    r"|social\s+media|seo\s+specialist|growth\s+(marketer|specialist)"
    r"|graphic\s+designer|ux\s+designer|visual\s+designer|art\s+director"
    # Finance / accounting / legal / admin
    r"|accountant|bookkeeper|controller|auditor|tax\s+(specialist|preparer)"
    r"|paralegal|legal\s+(assistant|secretary|counsel|intern)|attorney|lawyer"
    r"|executive\s+assistant|administrative\s+assistant|admin\s+assistant|receptionist|secretary"
    r"|office\s+(manager|coordinator|administrator)"
    # Operations / supply chain (non-engineering)
    r"|operations\s+(manager|associate|coordinator|specialist|director|analyst)"
    r"|supply\s+chain\s+(manager|analyst|coordinator)"
    r"|customer\s+(service|success|support)\s+(rep|representative|associate|specialist|coordinator)"
    r"|call\s+center|claims\s+(adjuster|processor|examiner)"
    # Teaching / childcare / coaching
    r"|teacher|tutor|instructor|professor|adjunct|preschool|childcare|babysitter"
    r"|coach|trainer|fitness"
    # Security / safety
    r"|security\s+(guard|officer|patrol)|loss\s+prevention"
    # Generic warehouse / production / agriculture
    r"|machine\s+operator|production\s+(worker|associate|operator)|assembler|farmhand|crop"
    r")\b",
    re.I,
)


def _is_tech_title(title: str) -> bool:
    """Return True when the job title looks like a tech role we want to keep.

    Algorithm: any allow-list match wins; otherwise reject on non-tech match;
    otherwise accept (ambiguous titles like "Project Manager" pass through and
    are filtered downstream by role_category/skills if needed).
    """
    if not title:
        return True   # let downstream validation reject empty titles
    t = title.strip()
    if _TECH_ALLOW_RE.search(t):
        return True
    if _NON_TECH_REJECT_RE.search(t):
        return False
    return True


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
                 max_retries=3, soft_time_limit=300)
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
        ).limit(5000)
        result = await db.execute(q)
        companies = list(result.scalars().all())

    dispatched = 0
    for company in companies:
        run_company_scan_task.delay(company.id)
        dispatched += 1

    logger.info("scan_new_companies_dispatched", count=dispatched, cutoff_hours=2)
    return {"dispatched": dispatched}


async def _promote_active_companies_async() -> dict:
    """Re-prioritise companies by *tech-job* volume (not raw job count).

    Ranking purely on jobs_found_count let a hospital Workday tenant posting
    500 nurse roles ride tier1 while a lean startup posting 5 engineering
    roles starved in tier3/tier4. We now score on how many *tech* jobs
    (role_category in RELEVANT_ROLES) a company produced recently, cap
    high-volume non-tech employers out of the fast tiers, and protect
    never-scanned companies so new tech companies get a fair chance before
    they can decay into the un-scanned pool. Load-neutral: total companies
    scanned per tick is still bounded by the tier limits — this only changes
    *which* companies the existing budget is spent on.
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
        stats = {r.company_id: (r.tech30, r.tech14, r.all30) for r in agg.all()}

        result = await db.execute(select(Company).where(Company.active == True))
        companies = list(result.scalars().all())

        for company in companies:
            old_score = company.priority_score
            tech30, tech14, all30 = stats.get(company.id, (0, 0, 0))
            never_scanned = company.last_success_at is None

            if tech14 >= 10 or tech30 >= 100:
                company.priority_score = max(company.priority_score, 90)
            elif tech30 >= 10:
                company.priority_score = max(company.priority_score, 60)
            elif tech30 >= 1:
                company.priority_score = max(company.priority_score, 35)
            elif tech30 == 0 and all30 >= 50:
                # High-volume but zero tech (hospital/retail tenant) → push
                # below tier3 (<20 ⇒ tier4) so it only gets the once-daily
                # sweep and the hourly+ budget stays on tech companies.
                company.priority_score = min(company.priority_score, 15)
                capped += 1
            elif never_scanned:
                # Never successfully scanned → protect so tier4-rescue +
                # scan-new can prove it before it can be demoted away.
                company.priority_score = max(company.priority_score, 25)
                protected += 1
            else:
                # Genuinely dead (scanned, no tech, low volume) → decay, but
                # never to 0 so the tier4 sweep can re-check it occasionally.
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

    # Tier-scan dispatch limit per tick.  Was 2000 — bumped to 10000 so the
    # long-tail of tier3 (which can reach 100k+ companies at full scale) gets
    # a full sweep within a couple of beat cycles instead of crawling for days.
    # The Redis broker holds these as reserved tasks; with worker concurrency
    # 12+ a 10k batch drains in ~15 min at 8s/scan.
    TIER_LIMIT = 10000

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
            # Oldest-due first (never-scanned NULLs first). Without this
            # ORDER BY Postgres returned an arbitrary slice each tick, so the
            # same companies were re-scanned while the rest starved forever.
            .order_by(
                Company.next_scan_at.asc().nulls_first(),
                Company.priority_score.desc(),
            )
            .limit(TIER_LIMIT)
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
                # Shallow (page-1 only) by DEFAULT so the whole ~74k corpus
                # is coverable daily on the 2-vCPU box. Full-depth pagination
                # is reserved for proven tech companies — tier1/tier2
                # (priority >= 60), which tech-aware promotion only grants to
                # companies actually posting tech jobs. Everyone else (the
                # ~70k tier3/tier4 bulk) gets the ~10-50x cheaper page-1 scan;
                # if one starts posting real tech jobs it gets promoted to
                # >=60 and graduates to full-depth automatically.
                shallow = company.priority_score < 60
                connector.shallow = shallow
                conn_result = await connector.fetch_jobs(company_id, company.ats_identifier)
                # connector.shallow only caps Workday's pagination. For every
                # other ATS a big company returns ALL its jobs in one response
                # — processing 1000s of them (normalize/classify/dedup/DB)
                # blows the task time limit. Cap processed jobs per company on
                # a shallow scan so the ~70k bulk stays cheap regardless of
                # ATS; proven tech companies (>=60) still process everything.
                if shallow and conn_result.jobs and len(conn_result.jobs) > 25:
                    conn_result.jobs = conn_result.jobs[:25]

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

            n_filtered_nontech = 0
            for raw_job in conn_result.jobs:
                raw_hash = hash_content(str(raw_job.raw_json))

                # ── Tech-only filter ──────────────────────────────────────
                # When INGEST_TECH_ONLY=1, drop obviously non-engineering
                # titles (cashier, nurse, driver, recruiter, ...) before they
                # consume Bronze storage and downstream enrichment cost.
                if INGEST_TECH_ONLY and not _is_tech_title(raw_job.title or ""):
                    n_filtered_nontech += 1
                    continue

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
            # Tier-based rescan interval: keep top companies fresh, but
            # re-scan the long tail far less often so the limited daily scan
            # budget covers many more *unique* companies (breadth > re-scan
            # frequency). max() so a longer per-company override still wins.
            _base = company.scan_frequency_minutes  # default 360 (6h)
            _ps = company.priority_score
            if _ps >= 90:
                _interval = _base            # ~6h  — tier1, stay fresh
            elif _ps >= 60:
                _interval = max(_base, 720)  # ~12h — tier2
            elif _ps >= 20:
                _interval = max(_base, 1440) # ~24h — tier3 (the bulk)
            else:
                _interval = max(_base, 2880) # ~48h — tier4 long tail
            company.next_scan_at = datetime.now(timezone.utc) + timedelta(
                minutes=_interval
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
                "checked=%d | inserted=%d | duplicates=%d | invalid=%d | non_tech_filtered=%d | duration=%.2fs",
                company.name, company.ats_type,
                len(conn_result.jobs),
                new_count,
                len(_batch_fps) - new_count,
                scan_invalid,
                n_filtered_nontech,
                time.monotonic() - start,
            )
            return {
                "company_id":         company_id,
                "company_name":       company.name,
                "jobs_fetched":       len(conn_result.jobs),
                "jobs_new":           new_count,
                "jobs_updated":       updated_count,
                "jobs_invalid":       scan_invalid,
                "non_tech_filtered":  n_filtered_nontech,
                "duration_s":         round(time.monotonic() - start, 2),
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
