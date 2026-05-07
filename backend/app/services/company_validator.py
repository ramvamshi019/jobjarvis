"""
Company Validation Engine.

Validates that a company is REAL, ACTIVE, and HIRING before allowing it
to consume ingestion resources.

Validation checks (in order, from cheapest to most expensive):
  1. Domain reachability (DNS + HTTP HEAD) — fast, no ATS call needed
  2. ATS endpoint probe — checks Greenhouse / Lever / Ashby / SmartRecruiters
  3. Career page fallback — HEAD-checks /careers, /jobs, etc.

Result is written directly to the Company row:
  - quality_score  →  stored in priority_score (existing column)
  - validation status  →  stored in notes ("validation:ok" / "validation:fail")
  - company.active  →  set False only if ALL checks fail after max_attempts

Design rules:
  - Pure async, no blocking I/O
  - All calls have hard timeouts
  - A single network hiccup never deactivates a company (threshold = 2)
  - Never modifies: title, description, required_skills, or safe-upsert logic
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.job import Job
from app.database import AsyncSessionLocal
from app.utils.domain_utils import normalize_domain

logger = logging.getLogger(__name__)

_TIMEOUT     = 8.0    # seconds for each network probe
_CAREER_PATHS = ["/careers", "/jobs", "/work-with-us", "/join-us", "/join"]
_MAX_ATTEMPTS = 2     # validation failures before deactivation

# ATS probe patterns — same as discovery but used for validation
_ATS_PROBES: list[tuple[str, str, str]] = [
    ("greenhouse",      "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",       "jobs"),
    ("lever",           "https://api.lever.co/v0/postings/{slug}?mode=json",            ""),
    ("ashby",           "https://api.ashbyhq.com/posting-api/job-board/{slug}",         "jobPostings"),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{slug}/postings", "content"),
]


# ── Result type ────────────────────────────────────────────────────────────────

class ValidationResult(NamedTuple):
    is_valid:        bool
    quality_score:   int       # 0–100 (stored in priority_score)
    reason:          str       # short diagnostic string
    ats_type:        str | None
    career_url:      str | None


# ── 1. Domain reachability ─────────────────────────────────────────────────────

async def _domain_resolves(domain: str) -> bool:
    """Non-blocking DNS check — runs getaddrinfo in executor to avoid blocking the loop."""
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, domain, 80),
            timeout=4.0,
        )
        return True
    except Exception:
        return False


async def _http_head(client: httpx.AsyncClient, url: str) -> int:
    """HEAD request; returns status code or 0 on error."""
    try:
        resp = await client.head(url, timeout=_TIMEOUT, follow_redirects=True)
        return resp.status_code
    except Exception:
        return 0


async def check_domain_reachable(client: httpx.AsyncClient, domain: str) -> bool:
    """
    Returns True if the domain resolves AND returns HTTP 2xx or 3xx.
    Parked / empty domains typically return 403 / 404 or don't resolve at all.
    """
    if not await _domain_resolves(domain):
        return False
    status = await _http_head(client, f"https://{domain}")
    if status == 0:
        # Try http as fallback
        status = await _http_head(client, f"http://{domain}")
    # Treat 2xx, 3xx as reachable; 4xx/5xx from a real server still counts
    # (many companies block HEAD at root but serve /careers fine).
    return status in range(200, 600) and status != 0


# ── 2. ATS probe ───────────────────────────────────────────────────────────────

async def _probe_ats(
    client: httpx.AsyncClient, slug: str
) -> tuple[str | None, int]:
    """
    Probe all ATS endpoints for slug.
    Returns (ats_type, job_count) of the first successful hit, or (None, 0).
    """
    sem = asyncio.Semaphore(4)

    async def _probe(ats_type: str, url_tpl: str, jobs_key: str):
        url = url_tpl.format(slug=slug)
        async with sem:
            try:
                resp = await client.get(url, timeout=_TIMEOUT)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                items = data.get(jobs_key, []) if jobs_key else (data if isinstance(data, list) else [])
                if items:
                    return (ats_type, len(items))
            except Exception:
                pass
            return None

    results = await asyncio.gather(
        *[_probe(at, tpl, key) for at, tpl, key in _ATS_PROBES],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, tuple):
            return r
    return (None, 0)


# ── 3. Career page check ───────────────────────────────────────────────────────

async def _find_career_page(client: httpx.AsyncClient, domain: str) -> str | None:
    """Return the first career path that returns HTTP 200, else None."""
    for path in _CAREER_PATHS:
        status = await _http_head(client, f"https://{domain}{path}")
        if status == 200:
            return f"https://{domain}{path}"
    return None


# ── 4. Quality scoring ─────────────────────────────────────────────────────────

def _compute_quality_score(
    domain_ok: bool,
    ats_type: str | None,
    job_count: int,
    career_url: str | None,
    recent_success: bool,
) -> int:
    """
    Returns 0–100 quality score.

    Weights:
      30  domain reachable
      30  ATS detected
      20  jobs found (≥1)
       5  career page exists
      15  recent activity (last_success_at within 7 days)
    """
    score = 0
    if domain_ok:    score += 30
    if ats_type:     score += 30
    if job_count > 0:score += 20
    if career_url:   score += 5
    if recent_success: score += 15
    return score


# ── Main validator ─────────────────────────────────────────────────────────────

async def validate_company(
    company: Company,
    client: httpx.AsyncClient | None = None,
) -> ValidationResult:
    """
    Full validation pass for one company.
    Uses an existing httpx client if provided (preferred for batch runs),
    or opens its own.

    Does NOT write to DB — caller decides what to persist.
    """
    domain = normalize_domain(company.domain or "")
    slug   = company.ats_identifier or ""

    async def _run(c: httpx.AsyncClient) -> ValidationResult:
        # Step 1: Domain reachability
        domain_ok = await check_domain_reachable(c, domain) if domain else False

        # Step 2: ATS probe
        ats_type, job_count = (None, 0)
        if slug:
            ats_type, job_count = await _probe_ats(c, slug)
        elif company.ats_type and company.ats_identifier:
            ats_type, job_count = await _probe_ats(c, company.ats_identifier)

        # Step 3: Career page (only if domain is reachable)
        career_url: str | None = None
        if domain_ok and not career_url:
            career_url = await _find_career_page(c, domain)

        # Step 4: Determine validity
        # A company is VALID if it passes at least ONE of:
        #   (a) ATS probe returned jobs
        #   (b) Career page exists
        #   (c) Domain is reachable AND had recent success
        recent_success = bool(
            company.last_success_at
            and (datetime.now(timezone.utc) - company.last_success_at) < timedelta(days=7)
        )

        is_valid = bool(job_count > 0 or career_url or (domain_ok and recent_success))

        # Reason tag for diagnostics
        if not domain_ok and not job_count and not career_url:
            reason = "domain_unreachable_no_ats"
        elif not domain_ok:
            reason = "domain_unreachable"
        elif job_count > 0:
            reason = f"ats_ok:{ats_type}:{job_count}_jobs"
        elif career_url:
            reason = "career_page_found"
        else:
            reason = "domain_ok_but_no_jobs"

        score = _compute_quality_score(domain_ok, ats_type, job_count, career_url, recent_success)

        return ValidationResult(
            is_valid=is_valid,
            quality_score=score,
            reason=reason,
            ats_type=ats_type,
            career_url=career_url,
        )

    if client:
        return await _run(client)
    else:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as c:
            return await _run(c)


# ── Batch validator ────────────────────────────────────────────────────────────

async def validate_and_update_companies(
    limit: int = 200,
    concurrency: int = 10,
) -> dict[str, int]:
    """
    Batch validation pass: validates up to `limit` companies and persists results.

    Priority order:
      1. Companies with no last_success_at (never validated)
      2. Companies with highest consecutive_failures

    Writes back to DB:
      - company.priority_score  ←  quality_score
      - company.notes           ←  last validation reason
      - company.active          ←  False if is_valid=False AND fails >= _MAX_ATTEMPTS
      - company.career_url      ←  if found and currently NULL
      - company.ats_type        ←  if discovered during validation
    """
    metrics = {
        "validated": 0,
        "valid":     0,
        "invalid":   0,
        "deactivated": 0,
    }

    # Load candidates
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Company)
            .where(Company.active == True)
            .order_by(
                Company.last_success_at.asc().nullsfirst(),
                Company.consecutive_failures.desc(),
            )
            .limit(limit)
        )
        companies = result.scalars().all()

    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as client:

        async def _validate_one(company: Company) -> None:
            async with sem:
                try:
                    vr = await validate_company(company, client)
                except Exception as exc:
                    logger.warning("validate_error company=%s error=%s", company.name, exc)
                    return

            async with AsyncSessionLocal() as db:
                c = await db.get(Company, company.id)
                if not c:
                    return

                # Always update quality score and notes
                c.priority_score = vr.quality_score
                c.notes = f"validation:{vr.reason}"

                # Update ATS if newly discovered
                if vr.ats_type and not c.ats_type:
                    c.ats_type = vr.ats_type

                # Update career_url if found and missing
                if vr.career_url and not c.career_url:
                    c.career_url = vr.career_url

                if vr.is_valid:
                    # Reset failure counter on successful validation
                    c.consecutive_failures = max(0, c.consecutive_failures - 1)
                    metrics["valid"] += 1
                else:
                    c.consecutive_failures += 1
                    metrics["invalid"] += 1
                    if c.consecutive_failures >= _MAX_ATTEMPTS:
                        c.active = False
                        logger.info(
                            "company_deactivated_by_validator name=%s reason=%s",
                            c.name, vr.reason,
                        )
                        metrics["deactivated"] += 1

                await db.commit()
                metrics["validated"] += 1

        await asyncio.gather(*[_validate_one(c) for c in companies])

    logger.info("batch_validation_complete metrics=%s", metrics)
    return metrics


# ── Auto-cleanup: no jobs in 7 days ───────────────────────────────────────────

async def cleanup_stale_companies() -> int:
    """
    Deactivate companies that have had no active jobs for 7+ days.
    Returns count deactivated.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    deactivated = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Company).where(
                Company.active == True,
                Company.last_success_at < cutoff,
            )
        )
        stale_companies = result.scalars().all()

        for company in stale_companies:
            # Verify no recent active jobs exist
            job_count_result = await db.execute(
                select(func.count(Job.id)).where(
                    Job.company_id == company.id,
                    Job.active == True,
                    Job.last_seen_at >= cutoff,
                )
            )
            recent_jobs = job_count_result.scalar() or 0

            if recent_jobs == 0:
                company.active = False
                company.notes = (company.notes or "") + " | auto_cleanup:no_recent_jobs"
                deactivated += 1
                logger.info(
                    "company_auto_deactivated name=%s last_success=%s",
                    company.name,
                    company.last_success_at,
                )

        if deactivated:
            await db.commit()

    logger.info("cleanup_stale_complete deactivated=%d", deactivated)
    return deactivated
