"""
Job ingestion pipeline — production hardened.

Per-company flow:
  1. Fetch raw jobs from ATS connector (with circuit breaker) or aggregator fallback
  2. For each raw job:
     a. Validate — reject records missing external_id, title, or company_id
     b. Normalise location → city / region / country / remote_type
     c. Classify role (title + description)
     d. Classify experience level
     e. Extract required / preferred skills from description
     f. Upsert in chunks of BATCH_SIZE=100 (each chunk its own transaction)
  3. Mark jobs unseen in this run as inactive

Key hardening changes:
  - BATCH_SIZE=100 prevents oversized SQL (PostgreSQL limit ~65 535 params)
  - Each chunk runs in its own DB savepoint; failure rolls back ONLY that chunk
  - Correct column mapping: Python "job_url" → SQL "url" at insert time
  - Strict validate_job() before any insert touches the DB
  - Full rollback on any exception; session is never left in a failed state
  - Fingerprint = sha256(title|company) only — no location drift
  - Structured logs: fetched / valid / inserted / skipped / failed per company
"""
from __future__ import annotations

import hashlib
import heapq
import logging
import asyncio
import os
import random
import time
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, case, func, and_, text
from sqlalchemy.dialects.postgresql import insert
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# Quiet noisy HTTP logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from app.database import AsyncSessionLocal
from app.models.company import Company
from app.models.job import Job
from app.connectors.ats import (
    fetch_jobs_from_ats,
    fetch_job_details,
    fetch_jobs_with_fallback,
    reset_ats_metrics,
    get_ats_metrics,
)
from app.connectors.aggregator import fetch_jobs_from_aggregator
from app.services.normalizer import (
    normalize_title,
    normalize_location,
    parse_location,
    normalize_country,
    normalize_remote,
    normalize_currency,
    classify_experience_level,
    classify_role_category,
    normalize_source_type,
    compute_fingerprint,
    compute_data_quality_score,
)
from app.ai.spam_detector import detect_spam
from app.ai.skill_extractor import extract_skills

logger = logging.getLogger(__name__)

# ── Tunables ───────────────────────────────────────────────────────────────────
BATCH_SIZE = 100           # rows per INSERT chunk — keeps param count safe
MAX_JOBS_PER_COMPANY = 500 # cap raw jobs fetched per company per run
                           # (was 100 — Bosch alone has 3,547 jobs, so 100 truncated
                           #  hard. 500 covers ~99% of employers; the rare 1000+ ones
                           #  get the tail on the next tier scan.)
_MAX_COMPANIES_PER_RUN = 500
_MICRO_BATCH_SIZE = 20     # companies per 60-second priority-queue tick
_CONCURRENCY = 12          # simultaneous per-company coroutines

# ── Dev / Ops overrides ────────────────────────────────────────────────────────
# DEV_FORCE_SCAN: when True (or env FORCE_SCAN=1), next_scan_at is completely
# ignored and all active companies are always selected.  Useful during local
# development and after database resets so the pipeline is never idle.
DEV_FORCE_SCAN: bool = os.getenv("FORCE_SCAN", "").lower() in ("1", "true", "yes")

# Companies whose next_scan_at is more than this many hours in the future are
# treated as having corrupted schedule data; their next_scan_at is reset to None
# so they are eligible immediately on the next run.
_MAX_NEXT_SCAN_DRIFT_HOURS: int = 24

# ── Circuit breaker ───────────────────────────────────────────────────────────
# provider → {"fails": int, "cooldown_until": float}
PROVIDER_STATE: dict = {}



# -- Per-provider observability -----------------------------------------------
# Tracks success/failure counts per ATS provider across all companies in a run.
# Reset at the start of each run alongside ATS connector metrics.
_PROVIDER_METRICS: dict = {}   # provider -> {"ok": int, "fail": int, "jobs": int, "time_s": float}


def _pm_record(provider: str, success: bool, jobs: int = 0, elapsed: float = 0.0) -> None:
    entry = _PROVIDER_METRICS.setdefault(provider, {"ok": 0, "fail": 0, "jobs": 0, "time_s": 0.0})
    if success:
        entry["ok"]   += 1
        entry["jobs"] += jobs
    else:
        entry["fail"] += 1
    entry["time_s"] += elapsed

# ── Issue 2: Junk title filter ────────────────────────────────────────────────
# Careers-page placeholders and navigation artifacts that aren't real jobs.
_JUNK_TITLE_SUBSTRINGS: tuple[str, ...] = (
    "careers page",
    "we are hiring",
    "join our team",
    "job openings",
    "open positions",
    "see all jobs",
    "view all jobs",
    "current openings",
    "no current openings",
    "check back later",
)

# ── Role filter — score-based (replaces strict category allowlist) ────────────
# Each title token adds 1 point; reject when total < 2.
# This accepts real-world DE titles the classifier might miss:
#   "Data Platform Engineer"(2), "ETL Developer"(1 — but etl alone +1 → total 1,
#   reject if no other signal), "Data Pipeline Engineer"(3), etc.
# Deliberately permissive: false-positives are caught by the spam gate.
def _role_score(title: str) -> int:
    """Return a 0-4 relevance score for data-engineering titles."""
    t = title.lower()
    score = 0
    if "data" in t:
        score += 1
    if "engineer" in t or "developer" in t:
        score += 1
    if "etl" in t or "pipeline" in t:
        score += 1
    if "analytics" in t or "analytical" in t:
        score += 1
    return score


# ── Lightweight job relevance scorer ─────────────────────────────────────────
# Returns 0.0–1.0. Gates expensive enrichment (description fetch) — jobs below
# the threshold get a synthesised description instead of a live HTTP call.
_RELEVANCE_KEYWORDS: frozenset[str] = frozenset({
    "data", "engineer", "pipeline", "etl", "analytics", "analytical",
    "spark", "airflow", "dbt", "kafka", "flink", "hadoop", "hive",
    "python", "sql", "bigquery", "redshift", "snowflake", "databricks",
    "machine learning", "ml", "deep learning", "ai", "nlp",
    "platform", "infrastructure", "backend", "cloud",
})

def score_job(title: str, description: str) -> float:
    """
    Keyword-based relevance score for a job posting.  0.0 = irrelevant,
    1.0 = highly relevant (data-engineering or ML role).

    Scoring:
      - Each keyword hit in the combined text adds weight.
      - Title hits are worth 2× description hits.
      - Normalised to [0, 1] with a soft cap at 8 hits → 1.0.
    """
    t = (title or "").lower()
    d = (description or "").lower()
    score = 0.0
    for kw in _RELEVANCE_KEYWORDS:
        if kw in t:
            score += 2.0
        elif kw in d:
            score += 1.0
    return min(1.0, score / 8.0)


# ── Smart company scan-priority score ────────────────────────────────────────
# Returns 0.0–1.0 representing how urgently we should re-scan this company.
# Written back to company.scan_priority after every successful pipeline run.
def compute_scan_priority(company) -> float:
    """
    Score = weighted blend of:
      - base priority_score (0–100 → 0.0–1.0)
      - recency of last job found (fresher = higher)
      - total jobs found count (prolific boards score higher)
      - penalty for consecutive failures
    """
    base = (getattr(company, "priority_score", 50) or 50) / 100.0

    # Recency bonus: full bonus if job found in last 24 h, decays over 7 days
    recency = 0.0
    ljf = getattr(company, "last_job_found_at", None)
    if ljf is not None:
        now = datetime.now(timezone.utc)
        hours_ago = (now - ljf.replace(tzinfo=timezone.utc) if ljf.tzinfo is None else now - ljf).total_seconds() / 3600
        recency = max(0.0, 1.0 - hours_ago / (7 * 24))

    # Volume signal: log-scale so 1 job = 0.1, 100 jobs = 0.5, 1000 = 0.75
    import math
    jobs_count = getattr(company, "jobs_found_count", 0) or 0
    volume = math.log1p(jobs_count) / math.log1p(1000)

    # Failure penalty
    failures = getattr(company, "consecutive_failures", 0) or 0
    penalty = min(1.0, failures * 0.1)

    raw = (base * 0.4) + (recency * 0.35) + (volume * 0.25) - penalty
    return round(max(0.0, min(1.0, raw)), 4)


# ── Experience filter — blocklist (safe, inclusive) ───────────────────────────
# Hard-reject only explicit seniority signals; None/unknown/junior/entry/mid all
# pass through.  classify_experience_level() may return any of these or None.
_EXP_HARD_REJECT: frozenset[str] = frozenset({"staff", "principal", "intern"})

# ── Freshness gate ────────────────────────────────────────────────────────────
# Only applied when posted_at is present and parseable.  Jobs with no posting
# date are always kept — unknown age ≠ stale (many ATS feeds omit the date).
# Override via env: JOB_FRESHNESS_HOURS=48
_FRESHNESS_CUTOFF_HOURS: int = int(os.getenv("JOB_FRESHNESS_HOURS", "24"))


# ── Return type ───────────────────────────────────────────────────────────────
class PipelineResult(TypedDict):
    fetched: int          # raw jobs from ATS/aggregator
    valid: int            # passed validate_job()
    inserted: int         # rows accepted by ON CONFLICT upsert
    skipped: int          # duplicates / quality-gated / intra-batch dedup
    failed: int           # chunks that raised an exception
    delta_stopped_at: int # index where delta break fired (-1 = processed all)
    delta_saved: int      # jobs bypassed by delta break


# ── HTTP helpers ──────────────────────────────────────────────────────────────
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.NetworkError, httpx.TimeoutException)),
    reraise=True,
)
async def _safe_fetch_job_details(
    client: httpx.AsyncClient,
    ats_type: str,
    ats_id: str,
    external_id: str,
) -> str:
    return await fetch_job_details(client, ats_type, ats_id, external_id)


# ── PHASE 2: Validation layer ─────────────────────────────────────────────────
def validate_job(job: dict, company_id: int) -> bool:
    """
    Return True only when all mandatory fields are present and non-empty.
    Rejects corrupted / incomplete records before they touch the DB.

    Rules:
      - company_id  must be a positive integer
      - external_id must be a non-empty string
      - title       must be a non-empty string
      - url         (job URL) must be non-empty if present we keep it,
                    but we don't hard-reject missing URL since external_id
                    uniquely identifies the job within a company
    """
    if not company_id or not isinstance(company_id, int):
        return False
    ext_id = job.get("external_id", "")
    if not ext_id or not str(ext_id).strip():
        return False
    title = job.get("title", "")
    if not title or not str(title).strip():
        return False
    return True


# ── Issue 2: Junk title detector ─────────────────────────────────────────────
def _is_junk_title(title: str) -> bool:
    """
    Return True when the title is a placeholder or navigation artifact.

    Checks:
      1. Any of _JUNK_TITLE_SUBSTRINGS appears in the lowercased title.
         Catches: "Careers Page moved", "We Are Hiring", "Join Our Team", etc.
      2. Title has fewer than 2 whitespace-separated tokens.
         Catches: "Jobs", "Apply", "Hiring" (single-word non-titles).

    Word-count threshold is intentionally 2, NOT 3.
    "Data Engineer" and "Analytics Engineer" are both 2-word valid job titles.
    Raising the threshold to 3 would silently drop the two most common target
    roles, which violates the DO-NOT-OVER-FILTER requirement.
    The keyword patterns in _JUNK_TITLE_SUBSTRINGS handle the multi-word junk
    cases ("We Are Hiring", "Join Our Team") independently.
    """
    if not title or not title.strip():
        return True
    t_lower = title.lower().strip()
    for sub in _JUNK_TITLE_SUBSTRINGS:
        if sub in t_lower:
            return True
    # Reject single-token titles only (< 2 words)
    if len(t_lower.split()) < 2:
        return True
    return False


# ── PHASE 6: Stable fingerprint ────────────────────────────────────────────────
def _fingerprint(title: str, company_name: str) -> str:
    """sha256(title|company) — location-agnostic, cross-ATS stable."""
    key = f"{title.lower().strip()}|{company_name.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:64]


# ── PHASE 7: Data cleaning ────────────────────────────────────────────────────
def _clean_str(val: Optional[str], sentinel: str = "") -> Optional[str]:
    """Convert empty strings to None; return sentinel if specified."""
    if val is None or str(val).strip() == "":
        return None if sentinel == "" else sentinel
    return str(val).strip()


def _clean_list(val) -> list:
    """Ensure JSON arrays are always a plain list, never None/invalid."""
    if not val:
        return []
    if isinstance(val, list):
        return val
    return []


def _clean_dict(val) -> dict:
    """Ensure JSON objects are always a plain dict, never None/invalid."""
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    return {}


# ── PHASE 3/4: Safe upsert helpers ───────────────────────────────────────────
def _safe_json(new_val, old_col):
    """Update JSON column only when new array has ≥ 1 element."""
    return case(
        (func.json_array_length(new_val) > 0, new_val),
        else_=old_col,
    )


def _safe_str(new_val, old_col, skip: tuple = ()):
    """Update string column only when new value is non-empty and not a sentinel."""
    if skip:
        cond = and_(new_val.is_not(None), new_val != "", new_val.notin_(skip))
    else:
        cond = and_(new_val.is_not(None), new_val != "")
    return case((cond, new_val), else_=old_col)


# ── PHASE 5: Per-chunk safe upsert ────────────────────────────────────────────
async def _upsert_chunk(
    db: AsyncSession,
    chunk: list[dict],
    company_name: str,
    run_now: datetime,
) -> tuple[int, int]:
    """
    Upsert a single chunk of job dicts.

    Each call runs inside its OWN savepoint so a failure here only rolls back
    this chunk — the outer session stays clean for the next chunk and for the
    stale-deactivation UPDATE.

    Returns (inserted_count, failed_count).
    """
    if not chunk:
        return 0, 0

    # ── PHASE 3: Correct column mapping ──────────────────────────────────────
    # The DB column is "url"; the pipeline dict key is "job_url".
    # We must rename here because SQLAlchemy INSERT uses actual column names.
    mapped = []
    for row in chunk:
        r = dict(row)
        # Rename job_url → url (matches DB column defined in migration)
        if "job_url" in r:
            r["url"] = r.pop("job_url")
        # Clean array/dict fields
        r["required_skills"]  = _clean_list(r.get("required_skills"))
        r["preferred_skills"] = _clean_list(r.get("preferred_skills"))
        r["matched_tools"]    = _clean_list(r.get("matched_tools"))
        # Strip empty-string sentinel values
        for str_col in ("country", "remote_type", "city", "region", "experience_level"):
            val = r.get(str_col)
            if isinstance(val, str) and val.strip() == "":
                r[str_col] = None
        mapped.append(r)

    try:
        async with db.begin_nested():  # savepoint — rollback only this chunk
            stmt = insert(Job).values(mapped)
            excl = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=["company_id", "external_id"],
                set_={
                    "last_seen_at":    run_now,
                    "active":          True,
                    "freshness_score": excl.freshness_score,
                    # PHASE 3: CASE WHEN guards — never overwrite good data
                    "fingerprint":      _safe_str(excl.fingerprint, Job.__table__.c.fingerprint),
                    "required_skills":  _safe_json(excl.required_skills,  Job.__table__.c.required_skills),
                    "preferred_skills": _safe_json(excl.preferred_skills, Job.__table__.c.preferred_skills),
                    "matched_tools":    _safe_json(excl.matched_tools,    Job.__table__.c.matched_tools),
                    "role_category":    _safe_str(excl.role_category,   Job.__table__.c.role_category,   skip=("Other",)),
                    "country":          _safe_str(excl.country,         Job.__table__.c.country,         skip=("unknown",)),
                    "remote_type":      _safe_str(excl.remote_type,     Job.__table__.c.remote_type,     skip=("unknown",)),
                    "city":             _safe_str(excl.city,            Job.__table__.c.city),
                    "region":           _safe_str(excl.region,          Job.__table__.c.region),
                    "experience_level": _safe_str(excl.experience_level, Job.__table__.c.experience_level),
                    "data_quality_score": excl.data_quality_score,
                },
            )
            await db.execute(stmt)
        return len(chunk), 0

    except Exception as exc:
        # Savepoint already rolled back — outer session is still usable
        logger.error(
            "[PIPELINE] chunk_insert_failed company=%s chunk_size=%d error=%s",
            company_name, len(chunk), exc,
        )
        return 0, len(chunk)


# ── Quality gate ──────────────────────────────────────────────────────────────
def _passes_quality_gate(raw_job: dict, spam_score: float) -> tuple[bool, str]:
    if not raw_job.get("title", "").strip():
        return False, "missing_title"
    if not raw_job.get("job_url", "").strip():
        return False, "missing_url"
    if spam_score >= 0.8:
        return False, f"high_spam_score={spam_score:.2f}"
    return True, "ok"


# ── Per-company processing (PHASES 2-8) ───────────────────────────────────────
async def process_company_jobs(
    db: AsyncSession,
    company: Company,
    client: httpx.AsyncClient,
    run_now: datetime,
) -> Optional[PipelineResult]:
    """
    Fetch, validate, enrich, and upsert all jobs for one company.
    Returns PipelineResult or None on total fetch failure.

    PHASE 5: Every DB write is wrapped in a savepoint. An exception in any
    chunk rolls back ONLY that chunk; the session stays clean for subsequent
    chunks and the stale-job UPDATE.
    """
    # Issue 5 + 6: aggregator is disabled; require a valid ATS configuration.
    # Companies that only have a domain but no ATS type/identifier are skipped
    # rather than falling back to the aggregator (which returned high-spam garbage).
    if not company.ats_type or not company.ats_identifier:
        logger.debug(
            "[PIPELINE] skip_no_ats company=%s ats_type=%s ats_id=%s",
            company.name, company.ats_type, company.ats_identifier,
        )
        return None

    ats_type = company.ats_type.lower() if company.ats_type else None

    # ── Circuit breaker (per-provider, in-process) ────────────────────────────
    # Trips only on network errors (not 404s).  Threshold: 10 failures.
    # Cooldown: 300 s (5 min).  During cooldown we still attempt fallback
    # providers so a single provider outage doesn't silence all companies.
    _cb_open = False
    if ats_type:
        pstate = PROVIDER_STATE.setdefault(ats_type, {"fails": 0, "cooldown_until": 0})
        if pstate["cooldown_until"] > time.time():
            _cb_open = True
            logger.warning(
                "[CB] circuit_breaker_open provider=%s company=%s — will attempt fallback",
                ats_type, company.name,
            )

    start_time = time.time()
    raw_jobs: list[dict] = []

    # ── ATS fetch with auto-detection fallback ────────────────────────────────
    # fetch_jobs_with_fallback() probes slug variations and alternate providers
    # (Greenhouse → Lever → Ashby) before giving up.  On success it returns the
    # working provider + slug so we can cache them back to the DB.
    if ats_type and company.ats_identifier:
        try:
            raw_jobs, used_ats, used_slug, fallback_used = await fetch_jobs_with_fallback(
                client,
                primary_ats=ats_type,
                primary_slug=company.ats_identifier,
                company_domain=getattr(company, "domain", None),
                skip_providers={ats_type} if _cb_open else None,
                company_id=company.id,
            )
            # Reset failure counter on any success
            PROVIDER_STATE.setdefault(ats_type, {"fails": 0, "cooldown_until": 0})["fails"] = 0
            _pm_record(used_ats, success=True, jobs=len(raw_jobs), elapsed=time.time() - start_time)

            if fallback_used:
                logger.info(
                    "[ATS_FALLBACK_USED] company=%s original_ats=%s original_slug=%s "
                    "working_ats=%s working_slug=%s jobs=%d",
                    company.name, ats_type, company.ats_identifier,
                    used_ats, used_slug, len(raw_jobs),
                )
                # Cache the working config so future runs use it directly
                company.ats_type = used_ats
                company.ats_identifier = used_slug
                ats_type = used_ats
            else:
                logger.info(
                    "[ATS_SUCCESS] company=%s ats=%s slug=%s jobs=%d",
                    company.name, used_ats, used_slug, len(raw_jobs),
                )

        except httpx.HTTPStatusError as exc:
            # 404 = all slug/provider combos returned "not found" → wrong config
            # Other HTTP errors = provider-side issue
            if exc.response.status_code != 404:
                pstate = PROVIDER_STATE.setdefault(ats_type, {"fails": 0, "cooldown_until": 0})
                pstate["fails"] += 1
                if pstate["fails"] >= 10:
                    pstate["cooldown_until"] = time.time() + 300
                    logger.error(
                        "[CB] circuit_breaker_triggered provider=%s after 10 fails — 5 min cooldown",
                        ats_type,
                    )
            logger.warning(
                "[PIPELINE] ats_fetch_failed company=%s status=%d error=%s",
                company.name, exc.response.status_code, exc,
            )
            _pm_record(ats_type or "unknown", success=False, elapsed=time.time() - start_time)

        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            # Network errors count against the circuit breaker
            pstate = PROVIDER_STATE.setdefault(ats_type, {"fails": 0, "cooldown_until": 0})
            pstate["fails"] += 1
            if pstate["fails"] >= 10:
                pstate["cooldown_until"] = time.time() + 300
                logger.error(
                    "[CB] circuit_breaker_triggered provider=%s after 10 fails — 5 min cooldown",
                    ats_type,
                )
            logger.warning(
                "[PIPELINE] ats_network_error company=%s error=%s fail_count=%d",
                company.name, exc, pstate["fails"],
            )
            _pm_record(ats_type or "unknown", success=False, elapsed=time.time() - start_time)

        except Exception as exc:
            logger.warning(
                "[PIPELINE] ats_fetch_error company=%s error=%s",
                company.name, exc,
            )
            _pm_record(ats_type or "unknown", success=False, elapsed=time.time() - start_time)

    # ── Aggregator fallback — DISABLED ───────────────────────────────────────
    # All companies require a valid ATS config; domain-only fallback returned
    # spam-heavy placeholder jobs and has been permanently disabled.
    if not raw_jobs and getattr(company, "domain", None):
        logger.debug(
            "[PIPELINE] aggregator_disabled company=%s — skipping domain fallback",
            company.name,
        )

    # ── Per-company fetch cap ─────────────────────────────────────────────────
    if len(raw_jobs) > MAX_JOBS_PER_COMPANY:
        logger.info(
            "[PIPELINE] fetch_cap_applied company=%s raw=%d capped=%d",
            company.name, len(raw_jobs), MAX_JOBS_PER_COMPANY,
        )
        raw_jobs = raw_jobs[:MAX_JOBS_PER_COMPANY]

    # ── Empty fetch ───────────────────────────────────────────────────────────
    if not raw_jobs:
        company.consecutive_failures = (company.consecutive_failures or 0) + 1
        company.failure_count        = (company.failure_count or 0) + 1
        # Deactivate only when ≥5 consecutive failures AND the company has
        # never had a successful fetch (last_success_at is None).
        # fetch_jobs_with_fallback already exhausted all providers + slug
        # variations, so this is a reliable signal of a broken ATS config.
        logger.debug(
            "[DEACTIVATE_CHECK] company=%s failures=%d",
            company.name, company.consecutive_failures,
        )
        if company.consecutive_failures >= 5 and company.last_success_at is None:
            company.active = False
            logger.warning(
                "[PIPELINE] company_deactivated company=%s consecutive_failures=%d "
                "(all ATS providers exhausted, never had a successful fetch)",
                company.name, company.consecutive_failures,
            )
        # Cooldown decay: no jobs returned → priority drops 20% each miss.
        # Dead companies naturally drift toward 0 and get scanned less often.
        company.scan_priority = max(0.0, round((company.scan_priority or 0.0) * 0.8, 4))
        logger.debug(
            "[PRIORITY_DECAY] company=%s scan_priority=%.4f failures=%d",
            company.name, company.scan_priority, company.consecutive_failures,
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
        return None

    # Successful fetch — reset failure streak, stamp last_success_at
    company.consecutive_failures = 0
    company.last_success_at      = run_now
    company.last_job_found_at    = run_now
    company.jobs_found_count     = (getattr(company, "jobs_found_count", 0) or 0) + len(raw_jobs)
    company.scan_priority        = compute_scan_priority(company)

    # Tiered scan frequency based on updated priority
    if company.scan_priority >= 0.7:
        company.scan_frequency_minutes = 5          # top tier: every 5 min
    elif company.scan_priority >= 0.4:
        company.scan_frequency_minutes = 30         # mid tier: every 30 min
    else:
        company.scan_frequency_minutes = 360        # low tier: every 6 h

    # ── Delta: load known external IDs for this company ──────────────────────
    # We query the 200 most-recently-seen external_ids for this company.
    # ATS feeds return jobs newest-first; the first hit against known_ids
    # signals that everything below it is already in the DB — we break early,
    # skipping expensive enrichment HTTP calls and upsert work for old data.
    known_ids: set[str] = set()
    try:
        _known_rows = await db.execute(
            select(Job.external_id)
            .where(Job.company_id == company.id)
            .where(Job.external_id.is_not(None))
            .order_by(Job.first_seen_at.desc())
            .limit(200)
        )
        known_ids = {str(r) for r in _known_rows.scalars().all()}
    except Exception as _kexc:
        logger.debug(
            "[DELTA] known_ids_load_failed company=%s error=%s",
            company.name, _kexc,
        )
    logger.debug(
        "[DELTA] company=%s known_ids_loaded=%d",
        company.name, len(known_ids),
    )

    # Delta tracking (set by the break below; -1 means we processed everything)
    _delta_stopped_at: int = -1
    _delta_saved: int      = 0

    # ── PHASE 2: Validation pass ──────────────────────────────────────────────
    # Also runs quality gate and spam detection before touching the DB
    jobs_to_insert: list[dict] = []
    seen_urls:  set[str] = set()
    seen_fps:   set[str] = set()
    rejected_counts: dict[str, int] = {}
    n_fetched = len(raw_jobs)
    n_enriched = 0

    for _job_idx, r_job in enumerate(raw_jobs):
        title       = normalize_title(r_job.get("title", ""))
        location_raw = r_job.get("location", "") or ""
        job_url      = r_job.get("job_url", "") or ""
        description  = r_job.get("description", "") or ""
        external_id  = r_job.get("external_id")

        # ── Delta break: stop at first already-known external_id ─────────────
        # This is the core efficiency win: skip enrichment + upsert for every
        # job that's already in the DB.  Works because ATS feeds are ordered
        # newest-first — the first known ID is the boundary.
        _eid_str = str(external_id).strip() if external_id else ""
        if _eid_str and _eid_str in known_ids:
            _delta_stopped_at = _job_idx
            _delta_saved      = n_fetched - _job_idx - 1
            logger.debug(
                "[DELTA_BREAK] company=%s stopped_at=%d saved=%d external_id=%s",
                company.name, _job_idx, _delta_saved, _eid_str,
            )
            break

        # ── Issue 2: Junk title check ─────────────────────────────────────────
        # Runs before any DB/network work to fail-fast on placeholder titles.
        if _is_junk_title(title):
            rejected_counts["junk_title"] = rejected_counts.get("junk_title", 0) + 1
            logger.debug(
                "[REJECTED_REASON] company=%s title=%r reason=junk_title",
                company.name, title,
            )
            continue

        # ── Intra-batch URL dedup ─────────────────────────────────────────────
        if job_url and job_url in seen_urls:
            rejected_counts["duplicate_url"] = rejected_counts.get("duplicate_url", 0) + 1
            logger.debug(
                "[REJECTED_REASON] company=%s title=%r reason=duplicate_url",
                company.name, title,
            )
            continue
        if job_url:
            seen_urls.add(job_url)

        # ── Spam detection ────────────────────────────────────────────────────
        r_job["company_domain"] = getattr(company, "domain", None)
        spam_res = detect_spam(r_job)
        if not description and "no_description" in spam_res.spam_flags:
            spam_res.spam_score = max(0.0, spam_res.spam_score - 0.9)

        # ── Quality gate ──────────────────────────────────────────────────────
        r_job["title"] = title
        passes, reason = _passes_quality_gate(r_job, spam_res.spam_score)
        if not passes:
            rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
            logger.debug(
                "[REJECTED_REASON] company=%s title=%r reason=%s",
                company.name, title, reason,
            )
            continue

        # ── Stable external_id ────────────────────────────────────────────────
        if not external_id or not str(external_id).strip():
            external_id = "hash_" + hashlib.sha256(
                f"{company.name}::{title}::{job_url}".lower().encode()
            ).hexdigest()[:16]

        # ── PHASE 2: validate_job ─────────────────────────────────────────────
        candidate = {
            "external_id": str(external_id),
            "title": title,
            "job_url": job_url,
        }
        if not validate_job(candidate, company.id):
            rejected_counts["validation_failed"] = rejected_counts.get("validation_failed", 0) + 1
            logger.debug(
                "[REJECTED_REASON] company=%s title=%r reason=validation_failed",
                company.name, title,
            )
            continue

        # ── Enrichment: fetch description if missing ──────────────────────────
        # score_job gates the HTTP call — low-relevance titles skip enrichment.
        _relevance = score_job(title, description)
        if not description and ats_type and company.ats_identifier and _relevance >= 0.3:
            try:
                description = await _safe_fetch_job_details(
                    client, ats_type, company.ats_identifier, str(external_id)
                )
                n_enriched += 1
            except Exception as exc:
                logger.debug(
                    "[PIPELINE] enrichment_failed company=%s id=%s error=%s",
                    company.name, external_id, exc,
                )

        # Fallback description from title
        if not description:
            description = f"Job title: {title}"

        # ── PHASE 7: Normalize all fields ─────────────────────────────────────
        location     = normalize_location(location_raw)
        city, region = parse_location(location_raw)
        country      = normalize_country(location_raw)
        remote_type  = normalize_remote(location_raw, title, description)
        role_cat     = classify_role_category(title, description)
        exp_level    = classify_experience_level(title, description)

        # ── Role filter: tiered scoring ───────────────────────────────────────
        # score >= 2 → HIGH PRIORITY, pass unconditionally
        # score == 1 → only pass if "engineer" is in the title
        #              (catches "Data Analyst", "BI Developer" noise)
        # score == 0 → reject
        _rs = _role_score(title)
        if _rs >= 2:
            pass  # high-confidence DE title
        elif _rs == 1:
            if "engineer" not in title.lower():
                _reject_key = "role_filtered:low_score_no_engineer"
                rejected_counts[_reject_key] = rejected_counts.get(_reject_key, 0) + 1
                logger.debug(
                    "[REJECTED_REASON] company=%s title=%r reason=low_score_no_engineer",
                    company.name, title,
                )
                continue
        else:
            _reject_key = "role_filtered:low_score"
            rejected_counts[_reject_key] = rejected_counts.get(_reject_key, 0) + 1
            logger.debug(
                "[REJECTED_REASON] company=%s title=%r reason=low_score score=0",
                company.name, title,
            )
            continue
        logger.debug("[LOW_SCORE_PASS] title=%s score=%d", title, _rs)

        # ── Experience filter (blocklist — safe) ──────────────────────────────
        # Hard-reject senior/staff/principal/intern only.
        # None, "", "unknown", "junior", "entry", "mid" all pass through.
        if exp_level in _EXP_HARD_REJECT:
            _reject_key = f"exp_filtered:{exp_level}"
            rejected_counts[_reject_key] = rejected_counts.get(_reject_key, 0) + 1
            logger.debug(
                "[REJECTED_REASON] company=%s title=%r reason=%s",
                company.name, title, _reject_key,
            )
            continue

        # ── PHASE 6: Fingerprint ──────────────────────────────────────────────
        fp = _fingerprint(title, company.name)
        if fp in seen_fps:
            rejected_counts["duplicate_fingerprint"] = rejected_counts.get("duplicate_fingerprint", 0) + 1
            logger.debug(
                "[REJECTED_REASON] company=%s title=%r reason=duplicate_fingerprint",
                company.name, title,
            )
            continue
        seen_fps.add(fp)

        # ── Skills & DQS ──────────────────────────────────────────────────────
        skills = extract_skills(title, description)
        dqs    = compute_data_quality_score(description, skills.required_skills, country, role_cat)

        posted_at = None
        posted_str = r_job.get("posted_at")
        if posted_str:
            try:
                posted_at = datetime.fromisoformat(str(posted_str).replace("Z", "+00:00"))
            except Exception:
                pass

        # ── Freshness gate ────────────────────────────────────────────────────
        # Only fires when posted_at is present and successfully parsed.
        # If posted_at is absent or unparseable we keep the job — unknown age
        # ≠ stale.  Many ATS feeds omit posting dates.
        #
        # Default cutoff: _FRESHNESS_CUTOFF_HOURS (24 h).
        # Override via env: JOB_FRESHNESS_HOURS=48
        #
        # Timezone safety: if the string had no tz info the result is naive;
        # treat naive datetimes as UTC before comparing with run_now (UTC-aware).
        if posted_at is not None:
            _pa_utc = (
                posted_at
                if posted_at.tzinfo is not None
                else posted_at.replace(tzinfo=timezone.utc)
            )
            if _pa_utc < (run_now - timedelta(hours=_FRESHNESS_CUTOFF_HOURS)):
                rejected_counts["posted_at_too_old"] = (
                    rejected_counts.get("posted_at_too_old", 0) + 1
                )
                logger.debug(
                    "[REJECTED_REASON] company=%s title=%r reason=posted_at_too_old "
                    "posted=%s cutoff_hours=%d",
                    company.name, title, _pa_utc.isoformat(), _FRESHNESS_CUTOFF_HOURS,
                )
                continue

        currency       = normalize_currency(description)
        source_conf    = 0.7 if r_job.get("source") == "aggregator_api" else 1.0
        src_type       = normalize_source_type(r_job.get("source_type") or r_job.get("source", ""))

        # ── Freshness score: max(0, 24 - hours_since_posted) ─────────────────
        # Normalised to [0, 1] → 0.0 = stale/unknown, 1.0 = posted < 1 h ago.
        if posted_at is not None:
            _pa_for_fresh = (
                posted_at if posted_at.tzinfo is not None
                else posted_at.replace(tzinfo=timezone.utc)
            )
            _hours_ago = max(0.0, (run_now - _pa_for_fresh).total_seconds() / 3600)
            _freshness_score = round(max(0.0, min(1.0, (24.0 - _hours_ago) / 24.0)), 4)
        else:
            _freshness_score = 0.5   # unknown age — treat as moderately fresh

        logger.debug(
            "[ACCEPTED] company=%s title=%r role=%s exp=%s country=%s remote=%s",
            company.name, title, role_cat, exp_level, country, remote_type,
        )

        jobs_to_insert.append({
            "company_id":          company.id,
            "company_name":        company.name,
            "external_id":         str(external_id),
            "title":               title,
            "normalized_title":    title.lower(),
            "location":            location or None,
            "normalized_location": location.lower() if location else None,
            "city":                city,
            "region":              region,
            "country":             country if country != "unknown" else None,
            "remote_type":         remote_type if remote_type != "unknown" else None,
            "job_url":             job_url or None,   # renamed → "url" at insert time
            "apply_url":           job_url or None,
            "description":         description,
            "experience_level":    exp_level,
            "role_category":       role_cat,
            "required_skills":     _clean_list(skills.required_skills),
            "preferred_skills":    _clean_list(skills.preferred_skills),
            "matched_tools":       _clean_list(skills.all_skills),
            "spam_score":          spam_res.spam_score,
            "data_quality_score":  dqs,
            "source":              r_job.get("source"),
            "source_type":         src_type,
            "source_confidence":   source_conf,
            "fingerprint":         fp,
            "salary_currency":     currency,
            "posted_at":           posted_at,
            "freshness_score":     _freshness_score,
            "active":              True,
            "last_seen_at":        run_now,
        })

    n_valid   = len(jobs_to_insert)
    n_skipped = n_fetched - n_valid

    # ── Delta efficiency report ───────────────────────────────────────────────
    if _delta_stopped_at >= 0:
        logger.info(
            "[DELTA_EFFICIENCY] company=%s fetched=%d stopped_at=%d saved=%d",
            company.name, n_fetched, _delta_stopped_at, _delta_saved,
        )

    # ── PHASE 8: Log validation summary ──────────────────────────────────────
    logger.info(
        "[PIPELINE] company=%s | fetched=%d | valid=%d | skipped=%d | reasons=%s",
        company.name, n_fetched, n_valid, n_skipped, rejected_counts or "none",
    )

    if not jobs_to_insert:
        return PipelineResult(
            fetched=n_fetched, valid=0, inserted=0, skipped=n_skipped, failed=0,
            delta_stopped_at=_delta_stopped_at, delta_saved=_delta_saved,
        )

    # ── PHASE 4: Chunked upsert (each chunk = one savepoint) ─────────────────
    n_inserted = 0
    n_failed   = 0

    for i in range(0, len(jobs_to_insert), BATCH_SIZE):
        chunk = jobs_to_insert[i : i + BATCH_SIZE]
        ins, fail = await _upsert_chunk(db, chunk, company.name, run_now)
        n_inserted += ins
        n_failed   += fail

    # ── Hot boost: reward companies that produced genuinely new rows ──────────
    # Boosting scan_priority ensures the next scheduler tick re-queues this
    # company near the top, triggering a follow-up scan within minutes.
    if n_inserted > 0:
        company.scan_priority = min(1.0, round((company.scan_priority or 0.0) + 0.2, 4))
        logger.info(
            "[HOT_COMPANY] company=%s boosted_priority=%.4f inserted=%d",
            company.name, company.scan_priority, n_inserted,
        )

    logger.info(
        "[PRIORITY_SCAN] company=%s priority=%.4f jobs_found=%d next_scan=%dmin",
        company.name, company.scan_priority, n_inserted,
        company.scan_frequency_minutes,
    )

    # ── PHASE 5: Stale-job deactivation (safe — session is clean) ────────────
    cutoff = run_now - timedelta(minutes=5)
    try:
        await db.execute(
            Job.__table__.update()
            .where(Job.company_id == company.id)
            .where(Job.last_seen_at < cutoff)
            .where(Job.active == True)
            .values(active=False, updated_at=run_now)
        )
        await db.commit()
    except Exception as exc:
        logger.error(
            "[PIPELINE] stale_deactivation_failed company=%s error=%s",
            company.name, exc,
        )
        await db.rollback()

    duration_s = time.time() - start_time
    logger.info(
        "[PIPELINE] scan_complete company=%s ats=%s | "
        "fetched=%d | valid=%d | inserted=%d | skipped=%d | failed=%d | enriched=%d | duration=%.2fs",
        company.name, ats_type or "aggregator",
        n_fetched, n_valid, n_inserted, n_skipped, n_failed, n_enriched, duration_s,
    )

    return PipelineResult(
        fetched=n_fetched,
        valid=n_valid,
        inserted=n_inserted,
        skipped=n_skipped,
        failed=n_failed,
        delta_stopped_at=_delta_stopped_at,
        delta_saved=_delta_saved,
    )


# ── Pipeline orchestration ────────────────────────────────────────────────────
async def run_ingestion_pipeline() -> None:
    """
    Main entry point.

    Company selection:
      1. Companies whose next_scan_at <= now (or NULL) are eligible.
      2. Sorted by priority_score DESC (Tier-1 first).
      3. Capped at _MAX_COMPANIES_PER_RUN per invocation.

    PHASE 10: Concurrency cap via asyncio.Semaphore(_CONCURRENCY).
    Each company runs in its own AsyncSessionLocal() so failures are isolated.
    """
    run_now        = datetime.now(timezone.utc)
    pipeline_start = time.time()

    # Reset per-run ATS metrics so each run produces clean numbers.
    reset_ats_metrics()
    _PROVIDER_METRICS.clear()

    logger.info("[PIPELINE] ingestion_pipeline_start")

    try:
        async with AsyncSessionLocal() as db:
            # ── Visibility: how many active companies exist at all? ───────────
            count_result = await db.execute(
                select(func.count(Company.id)).where(Company.active == True)
            )
            total_active: int = count_result.scalar() or 0
            logger.info("[PIPELINE] eligible_companies_total=%d", total_active)

            # ── Primary selection ─────────────────────────────────────────────
            # DEV_FORCE_SCAN (env FORCE_SCAN=1) bypasses next_scan_at entirely —
            # every active company is always selected regardless of schedule.
            if DEV_FORCE_SCAN:
                logger.info(
                    "[PIPELINE] DEV_FORCE_SCAN=True — ignoring next_scan_at, "
                    "selecting all %d active companies",
                    total_active,
                )
                primary_stmt = (
                    select(Company)
                    .where(Company.active == True)
                    .order_by(Company.scan_priority.desc(), Company.priority_score.desc())
                    .limit(_MAX_COMPANIES_PER_RUN)
                )
            else:
                primary_stmt = (
                    select(Company)
                    .where(Company.active == True)
                    .where(
                        (Company.next_scan_at == None) |  # noqa: E711
                        (Company.next_scan_at <= run_now)
                    )
                    .order_by(Company.scan_priority.desc(), Company.priority_score.desc())
                    .limit(_MAX_COMPANIES_PER_RUN)
                )

            result = await db.execute(primary_stmt)
            companies_raw = result.scalars().all()
            logger.info(
                "[PIPELINE] scheduled_companies_selected count=%d",
                len(companies_raw),
            )

            # ── Fallback: never go idle when companies exist ──────────────────
            # If the scheduling filter returns nothing but active companies DO
            # exist (all next_scan_at values are in the future), force-select the
            # top N by priority so the pipeline always does useful work.
            if not companies_raw and total_active > 0:
                logger.warning(
                    "[PIPELINE] fallback_triggered — 0 companies due for scan "
                    "(total_active=%d next_scan_at values all in future); "
                    "forcing scan of top %d by priority_score",
                    total_active, _MAX_COMPANIES_PER_RUN,
                )
                fallback_stmt = (
                    select(Company)
                    .where(Company.active == True)
                    .order_by(Company.scan_priority.desc(), Company.priority_score.desc())
                    .limit(_MAX_COMPANIES_PER_RUN)
                )
                fb_result = await db.execute(fallback_stmt)
                companies_raw = fb_result.scalars().all()

            # ── Priority queue: build max-heap ordered by scan_priority ───────
            # Python's heapq is a min-heap, so we negate the priority.
            # Secondary sort key: -priority_score (static tier as tiebreaker).
            # This guarantees Stripe (0.91) is submitted before a cold company
            # (0.12) even though asyncio.gather() runs them concurrently —
            # whichever hits the semaphore first gets processed first.
            _pq: list = [
                (-c.scan_priority, -c.priority_score, c.id)
                for c in companies_raw
            ]
            heapq.heapify(_pq)

            _cmap = {c.id: c for c in companies_raw}
            companies_data: list[dict] = []
            while _pq:
                _, _, cid = heapq.heappop(_pq)
                c = _cmap[cid]
                companies_data.append({
                    "id":       c.id,
                    "name":     c.name,
                    "freq":     c.scan_frequency_minutes,
                    "priority": c.scan_priority,
                })

            # Log queue state: top-5 companies about to be processed
            _top5 = [
                (c["name"], c["priority"])
                for c in companies_data[:5]
            ]
            logger.info("[QUEUE_STATE] top_5=%s", _top5)

        logger.info(
            "[PIPELINE] companies_selected count=%d (cap=%d)",
            len(companies_data), _MAX_COMPANIES_PER_RUN,
        )

        totals: dict = {
            "companies":                     0,
            "companies_successful":          0,
            "fetched":                       0,
            "valid":                         0,
            "inserted":                      0,
            "skipped":                       0,
            "failed":                        0,
            "errors":                        0,
            "delta_break_hits":              0,  # companies where delta break fired
            "delta_saved":                   0,  # total jobs skipped by delta breaks
            "jobs_skipped_early_old":        0,  # filled from _ATS_METRICS after gather
            "providers_skipped_by_cache":    0,  # filled from _ATS_METRICS after gather
            "providers_blocked_by_threshold":0,  # filled from _ATS_METRICS after gather
            "jobs_dropped_by_fetch_limit":   0,  # filled from _ATS_METRICS after gather
        }

        sem = asyncio.Semaphore(_CONCURRENCY)

        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "JobJarvis/2.0 (+https://jobjarvis.io/bot)"},
        ) as client:

            async def bounded_process(c_data: dict) -> None:
                async with sem:
                    # Rate-limit: stagger requests to avoid hammering ATS endpoints
                    await asyncio.sleep(random.uniform(0.2, 0.8))
                    try:
                        # PHASE 11: Each company has its own session
                        async with AsyncSessionLocal() as db:
                            c_obj = await db.get(Company, c_data["id"])
                            if not c_obj:
                                return

                            res = await process_company_jobs(db, c_obj, client, run_now)
                            totals["companies"] += 1

                            if res:
                                totals["fetched"]  += res["fetched"]
                                totals["valid"]    += res["valid"]
                                totals["inserted"] += res["inserted"]
                                totals["skipped"]  += res["skipped"]
                                totals["failed"]   += res["failed"]
                                if res["delta_stopped_at"] >= 0:
                                    totals["delta_break_hits"] += 1
                                totals["delta_saved"] += res["delta_saved"]
                                if res["fetched"] > 0:
                                    totals["companies_successful"] += 1

                            # ── Safety reset ─────────────────────────────────
                            # If next_scan_at is already absurdly far in the
                            # future (clock drift, bad seed data, manual edit),
                            # wipe it first so the company is eligible again on
                            # the very next run rather than being silently
                            # skipped for days/weeks.
                            if (
                                c_obj.next_scan_at is not None
                                and c_obj.next_scan_at
                                    > run_now + timedelta(hours=_MAX_NEXT_SCAN_DRIFT_HOURS)
                            ):
                                logger.warning(
                                    "[PIPELINE] next_scan_at_reset company=%s "
                                    "was=%s (>%dh ahead) — resetting to None",
                                    c_obj.name,
                                    c_obj.next_scan_at.isoformat(),
                                    _MAX_NEXT_SCAN_DRIFT_HOURS,
                                )
                                c_obj.next_scan_at = None

                            # Always advance scan timestamp — use the company's
                            # scan_frequency_minutes which may have just been
                            # updated by compute_scan_priority() above.
                            c_obj.last_checked_at = run_now
                            c_obj.next_scan_at    = run_now + timedelta(
                                minutes=c_obj.scan_frequency_minutes or c_data["freq"]
                            )
                            try:
                                await db.commit()
                            except Exception:
                                await db.rollback()

                    except Exception as exc:
                        totals["errors"] += 1
                        logger.error(
                            "[PIPELINE] company_fatal company=%s error=%s",
                            c_data["name"], exc,
                        )

            await asyncio.gather(*(bounded_process(c) for c in companies_data))

        # ── Merge ATS-level metrics collected inside connectors ───────────────
        ats_m = get_ats_metrics()
        totals["jobs_skipped_early_old"]         = ats_m["jobs_skipped_early_old"]
        totals["providers_skipped_by_cache"]     = ats_m["providers_skipped_by_cache"]
        totals["providers_blocked_by_threshold"] = ats_m["providers_blocked_by_threshold"]
        totals["jobs_dropped_by_fetch_limit"]    = ats_m["jobs_dropped_by_fetch_limit"]

        # ── Final summary ─────────────────────────────────────────────────────
        runtime = round(time.time() - pipeline_start, 2)
        logger.info(
            "[PIPELINE] ingestion_pipeline_complete | "
            "companies=%d | successful=%d | fetched=%d | valid=%d | inserted=%d | "
            "skipped=%d | failed=%d | errors=%d | runtime=%.1fs",
            totals["companies"],
            totals["companies_successful"],
            totals["fetched"],
            totals["valid"],
            totals["inserted"],
            totals["skipped"],
            totals["failed"],
            totals["errors"],
            runtime,
        )
        # [FINAL] block — easy to grep, matches required output format
        logger.info(
            "\n[FINAL]\n"
            "companies_scanned=%d\n"
            "companies_successful=%d\n"
            "jobs_fetched=%d\n"
            "jobs_valid=%d\n"
            "jobs_inserted=%d\n"
            "delta_break_hits=%d\n"
            "delta_jobs_saved=%d\n"
            "jobs_skipped_early_old=%d\n"
            "providers_skipped_by_cache=%d\n"
            "providers_blocked_by_threshold=%d\n"
            "jobs_dropped_by_fetch_limit=%d\n"
            "runtime=%.1fs",
            totals["companies"],
            totals["companies_successful"],
            totals["fetched"],
            totals["valid"],
            totals["inserted"],
            totals["delta_break_hits"],
            totals["delta_saved"],
            totals["jobs_skipped_early_old"],
            totals["providers_skipped_by_cache"],
            totals["providers_blocked_by_threshold"],
            totals["jobs_dropped_by_fetch_limit"],
            runtime,
        )
        print(
            f"\n[FINAL]\n"
            f"  companies_scanned={totals['companies']}\n"
            f"  companies_successful={totals['companies_successful']}\n"
            f"  jobs_fetched={totals['fetched']}\n"
            f"  jobs_valid={totals['valid']}\n"
            f"  jobs_inserted={totals['inserted']}\n"
            f"  delta_break_hits={totals['delta_break_hits']}\n"
            f"  delta_jobs_saved={totals['delta_saved']}\n"
            f"  jobs_skipped_early_old={totals['jobs_skipped_early_old']}\n"
            f"  providers_skipped_by_cache={totals['providers_skipped_by_cache']}\n"
            f"  providers_blocked_by_threshold={totals['providers_blocked_by_threshold']}\n"
            f"  jobs_dropped_by_fetch_limit={totals['jobs_dropped_by_fetch_limit']}\n"
            f"  runtime={runtime}s\n"
        )
        # Per-provider success-rate table
        if _PROVIDER_METRICS:
            rows = []
            for prov, m in sorted(_PROVIDER_METRICS.items()):
                total_calls = m["ok"] + m["fail"]
                rate = (m["ok"] / total_calls * 100) if total_calls else 0
                avg_t = (m["time_s"] / total_calls) if total_calls else 0
                rows.append(
                    f"  {prov:<18} ok={m['ok']} fail={m['fail']} "
                    f"success={rate:.0f}% jobs={m['jobs']} avg_t={avg_t:.2f}s"
                )
            logger.info("[PROVIDER_STATS]\n%s", "\n".join(rows))
            print("[PROVIDER_STATS]\n" + "\n".join(rows))

    except Exception as exc:
        logger.error("[PIPELINE] ingestion_pipeline_fatal error=%s", exc, exc_info=True)


async def run_priority_scan(batch_size: int = _MICRO_BATCH_SIZE) -> None:
    """
    Micro-batch priority scan — called every 60 seconds by the scheduler.

    Picks the top `batch_size` companies by scan_priority whose next_scan_at
    is due, processes them concurrently, then exits.  The scheduler calls this
    in a tight loop so the system continuously works on the highest-value
    sources rather than waiting for a 5-minute batch window.

    Hot companies (scan_priority > 0.8) skip the inter-request sleep so they
    get back-to-back follow-up scans during job spikes.

    Lifecycle of priorities across calls:
      new jobs inserted → +0.2 boost (see HOT_COMPANY log)
      empty fetch       → *0.8 decay (see PRIORITY_DECAY log)
    """
    run_now     = datetime.now(timezone.utc)
    batch_start = time.time()

    reset_ats_metrics()
    _PROVIDER_METRICS.clear()

    try:
        async with AsyncSessionLocal() as db:
            stmt = (
                select(Company)
                .where(Company.active == True)
                .where(
                    (Company.next_scan_at == None) |  # noqa: E711
                    (Company.next_scan_at <= run_now)
                )
                .order_by(Company.scan_priority.desc(), Company.priority_score.desc())
                .limit(batch_size)
            )
            result    = await db.execute(stmt)
            companies_raw = list(result.scalars().all())

        if not companies_raw:
            logger.debug("[PRIORITY_SCAN] no_companies_due batch_size=%d", batch_size)
            return

        # ── Build priority heap ───────────────────────────────────────────────
        _pq: list = [(-c.scan_priority, -c.priority_score, c.id) for c in companies_raw]
        heapq.heapify(_pq)
        _cmap = {c.id: c for c in companies_raw}

        _top5 = [(c.name, round(c.scan_priority, 3))
                 for c in sorted(companies_raw, key=lambda x: -x.scan_priority)[:5]]
        logger.info(
            "[QUEUE_STATE] batch_size=%d due=%d top_5=%s",
            batch_size, len(companies_raw), _top5,
        )

        # Extract in priority order; tag hot companies for fast-path
        companies_data: list[dict] = []
        while _pq:
            _, _, cid = heapq.heappop(_pq)
            c = _cmap[cid]
            companies_data.append({
                "id":        c.id,
                "name":      c.name,
                "freq":      c.scan_frequency_minutes,
                "priority":  c.scan_priority,
                "skip_sleep": c.scan_priority > 0.8,  # hot path: no rate-limit delay
            })

        totals: dict = {
            "companies": 0, "fetched": 0, "inserted": 0,
            "delta_break_hits": 0, "delta_saved": 0, "errors": 0,
        }

        sem = asyncio.Semaphore(_CONCURRENCY)

        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "JobJarvis/2.0 (+https://jobjarvis.io/bot)"},
        ) as client:

            async def _priority_bounded(c_data: dict) -> None:
                async with sem:
                    # Hot companies skip the rate-limit sleep — they're
                    # actively posting jobs and every second counts.
                    if not c_data.get("skip_sleep"):
                        await asyncio.sleep(random.uniform(0.2, 0.8))
                    try:
                        async with AsyncSessionLocal() as db:
                            c_obj = await db.get(Company, c_data["id"])
                            if not c_obj:
                                return

                            res = await process_company_jobs(db, c_obj, client, run_now)
                            totals["companies"] += 1

                            if res:
                                totals["fetched"]   += res["fetched"]
                                totals["inserted"]  += res["inserted"]
                                if res["delta_stopped_at"] >= 0:
                                    totals["delta_break_hits"] += 1
                                totals["delta_saved"] += res["delta_saved"]

                            # Safety reset for runaway next_scan_at
                            if (
                                c_obj.next_scan_at is not None
                                and c_obj.next_scan_at
                                    > run_now + timedelta(hours=_MAX_NEXT_SCAN_DRIFT_HOURS)
                            ):
                                c_obj.next_scan_at = None

                            c_obj.last_checked_at = run_now
                            c_obj.next_scan_at    = run_now + timedelta(
                                minutes=c_obj.scan_frequency_minutes or c_data["freq"]
                            )
                            try:
                                await db.commit()
                            except Exception:
                                await db.rollback()

                    except Exception as exc:
                        totals["errors"] += 1
                        logger.error(
                            "[PRIORITY_SCAN] company_fatal company=%s error=%s",
                            c_data["name"], exc,
                        )

            await asyncio.gather(*(_priority_bounded(c) for c in companies_data))

        runtime = round(time.time() - batch_start, 2)
        logger.info(
            "[PRIORITY_SCAN] batch_complete companies=%d fetched=%d inserted=%d "
            "delta_breaks=%d delta_saved=%d errors=%d runtime=%.1fs",
            totals["companies"], totals["fetched"], totals["inserted"],
            totals["delta_break_hits"], totals["delta_saved"],
            totals["errors"], runtime,
        )

    except Exception as exc:
        logger.error("[PRIORITY_SCAN] fatal error=%s", exc, exc_info=True)


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(run_ingestion_pipeline())
