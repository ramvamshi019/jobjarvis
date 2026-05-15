"""
Multi-source job-board scraping via python-jobspy.

Covers what our ATS-based scanners can't: mega-employers (Amazon, Google,
Apple, Meta) whose own portals broke or are bot-locked, but who still post
to Indeed / LinkedIn / Glassdoor / ZipRecruiter.  Each scheduled task hits
one board, walks several tech search terms, and feeds the results through
the same `_upsert_jobs` helper used by the rest of the job-board pipeline.

Boards & cadence:
  • Indeed       — every 2 hours (most reliable, biggest volume)
  • LinkedIn     — every 6 hours (rate-limited; we stay conservative)
  • Glassdoor    — daily (anti-bot is aggressive — we only need a daily sweep)
  • ZipRecruiter — every 4 hours (small but unique long-tail US employers)

All free, no auth required.  python-jobspy is the maintained scraper that
handles each board's bot defenses (TLS fingerprinting, cookies, etc.).  If
any board throws (rate limit, captcha), the failure is logged and the next
board still runs — one bad scrape never breaks the others.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import structlog

from app.workers.celery_app import celery_app
from app.workers.jobboard_tasks import _upsert_jobs, _run_async

logger = structlog.get_logger(__name__)

# Lazy import — python-jobspy is a heavy dep with pandas; only loaded when a
# task actually runs.
_JOBSPY_AVAILABLE: bool | None = None


def _load_jobspy():
    global _JOBSPY_AVAILABLE
    try:
        from jobspy import scrape_jobs  # type: ignore
        _JOBSPY_AVAILABLE = True
        return scrape_jobs
    except ImportError as e:
        _JOBSPY_AVAILABLE = False
        logger.warning("jobspy_import_failed", err=str(e))
        return None


# ── Tech-focused search terms ────────────────────────────────────────────────
# Each board accepts a single `search_term`; we sweep across these for
# breadth.  Per term we cap results to keep each board's session short
# enough to avoid rate-limiting.
_TECH_TERMS: list[str] = [
    "software engineer",
    "data engineer",
    "machine learning engineer",
    "AI engineer",
    "backend engineer",
    "platform engineer",
    "site reliability engineer",
    "data scientist",
    "ML engineer",
    "research engineer",
]

_RESULTS_PER_TERM: int = 50   # jobs requested per search-term per board run
_US_LOCATION: str = "United States"


# ── DataFrame → upsert-dict mapping ──────────────────────────────────────────

def _row_to_job(row: dict, source: str) -> dict | None:
    """Convert one python-jobspy DataFrame row into our upsert schema.

    JobSpy fields we map:
      title, company, location, job_url, description,
      date_posted, min_amount, max_amount, currency, interval,
      site (Indeed/LinkedIn/...), job_type, is_remote, country
    """
    title = (row.get("title") or "").strip()
    company = (row.get("company") or "").strip()
    url = (row.get("job_url") or "").strip()
    if not title or not company or not url:
        return None

    # JobSpy's `date_posted` is a python date; normalize to UTC datetime
    posted_at = None
    dp = row.get("date_posted")
    if dp:
        try:
            from datetime import date, datetime as _dt
            if isinstance(dp, date) and not isinstance(dp, _dt):
                posted_at = _dt.combine(dp, _dt.min.time()).replace(tzinfo=timezone.utc)
            elif isinstance(dp, _dt):
                posted_at = dp if dp.tzinfo else dp.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    # Salary range, both columns are usually floats or None
    smin = row.get("min_amount")
    smax = row.get("max_amount")
    currency = (row.get("currency") or "USD")[:3]
    try:
        smin = float(smin) if smin is not None else None
        smax = float(smax) if smax is not None else None
    except (TypeError, ValueError):
        smin = smax = None

    remote = None
    try:
        if row.get("is_remote") is True:
            remote = "remote"
    except Exception:
        pass

    return {
        "external_id":  str(row.get("id") or "")[:200] or None,
        "title":        title[:500],
        "company_name": company[:500],
        "description":  (row.get("description") or "")[:5000],
        "location":     (row.get("location") or "")[:500] or None,
        "country":      (row.get("country") or "US")[:100],
        "remote_type":  remote,
        "job_url":      url[:2000],
        "employment_type": (row.get("job_type") or "")[:100] or None,
        "salary_min":   smin,
        "salary_max":   smax,
        "salary_currency": currency,
        "posted_at":    posted_at,
        "source":       source,
    }


# ── Per-board fetch helpers ──────────────────────────────────────────────────

def _run_board_scrape(site_name: str, hours_old: int, results_per_term: int) -> int:
    """Sync helper — sweep _TECH_TERMS for one board, upsert results.

    Returns inserted count.  Synchronous because python-jobspy is sync; we
    invoke it from the async celery task wrapper.
    """
    scrape = _load_jobspy()
    if not scrape:
        return 0

    total_rows: list[dict] = []
    for term in _TECH_TERMS:
        try:
            df = scrape(
                site_name=[site_name],
                search_term=term,
                location=_US_LOCATION,
                results_wanted=results_per_term,
                hours_old=hours_old,
                country_indeed="USA" if site_name == "indeed" else None,
                verbose=0,
            )
        except Exception as e:
            logger.warning("jobspy_scrape_failed", site=site_name, term=term, err=str(e)[:120])
            continue

        # df may be a pandas DataFrame or None
        if df is None or len(df) == 0:
            continue
        try:
            for _, row in df.iterrows():
                d = _row_to_job(row.to_dict(), source=f"jobspy_{site_name}")
                if d:
                    total_rows.append(d)
        except Exception as e:
            logger.warning("jobspy_row_iter_failed", site=site_name, term=term, err=str(e)[:120])

    if not total_rows:
        logger.info("jobspy_no_rows", site=site_name)
        return 0

    inserted = asyncio.run(_upsert_jobs(total_rows, source=f"jobspy_{site_name}"))
    logger.info("jobspy_board_done",
                site=site_name, fetched=len(total_rows), inserted=inserted)
    return inserted


# ── Celery tasks ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.workers.jobspy_tasks.fetch_indeed",
    soft_time_limit=1800, max_retries=1,
)
def fetch_indeed() -> dict:
    """Indeed sweep — every 2 h.  Highest-volume source."""
    n = _run_board_scrape("indeed", hours_old=24, results_per_term=_RESULTS_PER_TERM)
    return {"site": "indeed", "inserted": n}


@celery_app.task(
    name="app.workers.jobspy_tasks.fetch_linkedin",
    soft_time_limit=1800, max_retries=1,
)
def fetch_linkedin() -> dict:
    """LinkedIn sweep — every 6 h.  Smaller request budget; LinkedIn rate-
    limits aggressively, so we ask for fewer results per term."""
    n = _run_board_scrape("linkedin", hours_old=24, results_per_term=25)
    return {"site": "linkedin", "inserted": n}


@celery_app.task(
    name="app.workers.jobspy_tasks.fetch_glassdoor",
    soft_time_limit=1800, max_retries=1,
)
def fetch_glassdoor() -> dict:
    """Glassdoor sweep — daily.  Aggressive anti-bot, daily is plenty."""
    n = _run_board_scrape("glassdoor", hours_old=48, results_per_term=25)
    return {"site": "glassdoor", "inserted": n}


@celery_app.task(
    name="app.workers.jobspy_tasks.fetch_ziprecruiter",
    soft_time_limit=1800, max_retries=1,
)
def fetch_ziprecruiter() -> dict:
    """ZipRecruiter sweep — every 4 h.  Smaller but covers long-tail US."""
    n = _run_board_scrape("zip_recruiter", hours_old=24, results_per_term=_RESULTS_PER_TERM)
    return {"site": "ziprecruiter", "inserted": n}


@celery_app.task(
    name="app.workers.jobspy_tasks.fetch_all_boards",
    soft_time_limit=7200, max_retries=0,
)
def fetch_all_boards() -> dict:
    """Fire every board sequentially.  Used by admin endpoint / startup."""
    out: list[dict] = []
    for fn in (fetch_indeed, fetch_linkedin, fetch_glassdoor, fetch_ziprecruiter):
        try:
            out.append(fn.run())  # type: ignore[attr-defined]
        except Exception as e:
            logger.exception("jobspy_step_failed", step=fn.name, err=str(e))
    total = sum(r.get("inserted", 0) for r in out)
    logger.info("jobspy_all_boards_done", total=total, per_board=out)
    return {"per_board": out, "total_inserted": total}
