"""
Job board API integrations — pulls jobs directly from public boards.

Sources (all free, no scraping):
  - RemoteOK        : remoteok.com/api           (no auth)
  - The Muse        : themuse.com/api/public/jobs (no auth)
  - Arbeitnow       : arbeitnow.com/api           (no auth, EU-focused)
  - HackerNews      : Algolia HN API, "Who is Hiring" monthly thread
  - Adzuna          : api.adzuna.com              (free key, ADZUNA_APP_ID + ADZUNA_API_KEY)

Each fetcher normalises data into a common dict, then _upsert_jobs() writes
to the DB — deduplicating on job_url so re-runs are safe.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.workers.celery_app import celery_app
from app.database import AsyncSessionLocal, async_engine
from app.models.company import Company
from app.models.job import Job

logger = structlog.get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_API_KEY = os.getenv("ADZUNA_API_KEY", "")

_HEADERS = {"User-Agent": "JobJarvis/1.0 ramvamshikrishna0@gmail.com"}


# ── Celery task entry points ──────────────────────────────────────────────────

def _run_async(coro):
    async def _wrapper():
        await async_engine.dispose()
        return await coro
    return asyncio.run(_wrapper())


@celery_app.task(name="app.workers.jobboard_tasks.fetch_all_boards",
                 soft_time_limit=3600, max_retries=1)
def fetch_all_boards():
    """Fetch from all configured job boards in one pass."""
    return _run_async(_fetch_all_async())


@celery_app.task(name="app.workers.jobboard_tasks.fetch_remoteok",
                 soft_time_limit=600, max_retries=2)
def fetch_remoteok():
    return _run_async(_fetch_and_store("remoteok", _fetch_remoteok))


@celery_app.task(name="app.workers.jobboard_tasks.fetch_themuse",
                 soft_time_limit=900, max_retries=2)
def fetch_themuse():
    return _run_async(_fetch_and_store("themuse", _fetch_themuse))


@celery_app.task(name="app.workers.jobboard_tasks.fetch_arbeitnow",
                 soft_time_limit=600, max_retries=2)
def fetch_arbeitnow():
    return _run_async(_fetch_and_store("arbeitnow", _fetch_arbeitnow))


@celery_app.task(name="app.workers.jobboard_tasks.fetch_hn_hiring",
                 soft_time_limit=600, max_retries=2)
def fetch_hn_hiring():
    return _run_async(_fetch_and_store("hn_hiring", _fetch_hn_hiring))


@celery_app.task(name="app.workers.jobboard_tasks.fetch_adzuna",
                 soft_time_limit=1800, max_retries=2)
def fetch_adzuna():
    return _run_async(_fetch_and_store("adzuna", _fetch_adzuna))


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def _fetch_all_async() -> dict:
    results = {}
    sources = [
        ("remoteok",  _fetch_remoteok),
        ("themuse",   _fetch_themuse),
        ("arbeitnow", _fetch_arbeitnow),
        ("hn_hiring", _fetch_hn_hiring),
    ]
    if ADZUNA_APP_ID and ADZUNA_API_KEY:
        sources.append(("adzuna", _fetch_adzuna))

    for name, fetcher in sources:
        try:
            result = await _fetch_and_store(name, fetcher)
            results[name] = result
        except Exception as exc:
            logger.error("jobboard_fetch_error", source=name, error=str(exc))
            results[name] = {"error": str(exc)}

    total = sum(r.get("inserted", 0) for r in results.values() if isinstance(r, dict))
    logger.info("all_boards_complete", total_inserted=total, breakdown=results)
    return {"total_inserted": total, "breakdown": results}


async def _fetch_and_store(source_name: str, fetcher) -> dict:
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
        jobs = await fetcher(client)

    logger.info("jobboard_fetched", source=source_name, count=len(jobs))
    inserted = await _upsert_jobs(jobs, source_name)
    logger.info("jobboard_stored", source=source_name, inserted=inserted)
    return {"fetched": len(jobs), "inserted": inserted}


# ── RemoteOK ──────────────────────────────────────────────────────────────────

async def _fetch_remoteok(client: httpx.AsyncClient) -> list[dict]:
    """https://remoteok.com/api — free JSON, no auth."""
    jobs = []
    try:
        r = await client.get("https://remoteok.com/api", timeout=20)
        if r.status_code != 200:
            logger.warning("remoteok_error", status=r.status_code)
            return []
        data = r.json()
        # First item is a legal notice dict, skip it
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            title   = (item.get("position") or "").strip()
            company = (item.get("company") or "").strip()
            url     = (item.get("url") or "").strip()
            if not title or not company or not url:
                continue

            # Parse tags for skills
            tags = item.get("tags") or []
            skills = [t for t in tags if isinstance(t, str) and len(t) < 40][:15]

            # Parse salary from description
            desc = item.get("description") or ""
            salary_min, salary_max = _parse_salary(desc)

            posted_raw = item.get("date") or item.get("epoch")
            posted_at = _parse_date(posted_raw)

            jobs.append({
                "title":          title,
                "company_name":   company,
                "job_url":        url,
                "description":    desc,
                "location":       "Remote",
                "remote_type":    "remote",
                "required_skills": skills,
                "posted_at":      posted_at,
                "source":         "remoteok",
                "external_id":    str(item.get("id", "")),
                "salary_min":     salary_min,
                "salary_max":     salary_max,
                "salary_currency": "USD",
            })
    except Exception as exc:
        logger.error("remoteok_parse_error", error=str(exc))
    return jobs


# ── The Muse ──────────────────────────────────────────────────────────────────

async def _fetch_themuse(client: httpx.AsyncClient) -> list[dict]:
    """https://www.themuse.com/api/public/jobs — free, no auth, paginated."""
    jobs = []
    page = 1
    while page <= 20:   # cap at 20 pages = ~2000 jobs
        try:
            r = await client.get(
                "https://www.themuse.com/api/public/jobs",
                params={"page": page, "api_key": ""},
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            results = data.get("results", [])
            if not results:
                break
            for item in results:
                title   = (item.get("name") or "").strip()
                company = (item.get("company", {}) or {}).get("name", "").strip()
                url     = (item.get("refs", {}) or {}).get("landing_page", "").strip()
                if not title or not company or not url:
                    continue

                # Location
                locs = item.get("locations") or []
                location = locs[0].get("name", "") if locs else ""
                remote_type = "remote" if "remote" in location.lower() else None

                # Categories → role hint
                cats = [c.get("name", "") for c in (item.get("categories") or [])]

                # Levels
                levels = [l.get("name", "").lower() for l in (item.get("levels") or [])]
                exp = None
                if any("senior" in l for l in levels):      exp = "senior"
                elif any("junior" in l or "entry" in l for l in levels): exp = "entry"
                elif any("mid" in l for l in levels):        exp = "mid"

                posted_at = _parse_date(item.get("publication_date"))

                jobs.append({
                    "title":            title,
                    "company_name":     company,
                    "job_url":          url,
                    "description":      item.get("contents") or "",
                    "location":         location,
                    "remote_type":      remote_type,
                    "experience_level": exp,
                    "role_category":    cats[0] if cats else None,
                    "posted_at":        posted_at,
                    "source":           "themuse",
                    "external_id":      str(item.get("id", "")),
                })
            page += 1
            if page > data.get("page_count", 1):
                break
        except Exception as exc:
            logger.error("themuse_parse_error", page=page, error=str(exc))
            break
    return jobs


# ── Arbeitnow ─────────────────────────────────────────────────────────────────

async def _fetch_arbeitnow(client: httpx.AsyncClient) -> list[dict]:
    """https://www.arbeitnow.com/api/job-board-api — free, no auth, EU-focused."""
    jobs = []
    page = 1
    while page <= 10:
        try:
            r = await client.get(
                "https://www.arbeitnow.com/api/job-board-api",
                params={"page": page},
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("data", [])
            if not items:
                break
            for item in items:
                title   = (item.get("title") or "").strip()
                company = (item.get("company_name") or "").strip()
                url     = (item.get("url") or "").strip()
                if not title or not company or not url:
                    continue

                location = (item.get("location") or "").strip()
                remote   = item.get("remote", False)

                tags = item.get("tags") or []
                skills = [t for t in tags if isinstance(t, str)][:15]

                posted_at = _parse_date(item.get("created_at"))

                jobs.append({
                    "title":           title,
                    "company_name":    company,
                    "job_url":         url,
                    "description":     item.get("description") or "",
                    "location":        location,
                    "remote_type":     "remote" if remote else None,
                    "required_skills": skills,
                    "posted_at":       posted_at,
                    "source":          "arbeitnow",
                    "external_id":     str(item.get("slug", "")),
                    "employment_type": item.get("job_types", [None])[0] if item.get("job_types") else None,
                })
            page += 1
        except Exception as exc:
            logger.error("arbeitnow_parse_error", page=page, error=str(exc))
            break
    return jobs


# ── HackerNews "Who is Hiring" ────────────────────────────────────────────────

async def _fetch_hn_hiring(client: httpx.AsyncClient) -> list[dict]:
    """
    Parse the monthly HN 'Ask HN: Who is hiring?' thread via Algolia API.
    Each top-level comment is a job posting. Typically ~2000 jobs/month, high quality.
    """
    jobs = []
    try:
        # Find the latest "Who is hiring" thread
        r = await client.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": "Ask HN: Who is hiring?",
                "tags":  "story,ask_hn",
                "numericFilters": "created_at_i>1700000000",
                "hitsPerPage": 5,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return []

        hits = r.json().get("hits", [])
        if not hits:
            return []

        # Use the most recent thread
        thread_id = hits[0].get("objectID")
        if not thread_id:
            return []

        # Fetch all comments (job postings) from that thread
        page = 0
        while page < 20:
            cr = await client.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "tags":         f"comment,story_{thread_id}",
                    "hitsPerPage":  100,
                    "page":         page,
                },
                timeout=15,
            )
            if cr.status_code != 200:
                break
            cdata  = cr.json()
            comments = cdata.get("hits", [])
            if not comments:
                break

            for c in comments:
                text = c.get("comment_text") or c.get("story_text") or ""
                if not text or len(text) < 50:
                    continue

                # Parse "Company | Role | Location | ..." format
                parsed = _parse_hn_comment(text, c.get("objectID", ""))
                if parsed:
                    jobs.append(parsed)

            page += 1
            if page >= cdata.get("nbPages", 1):
                break

    except Exception as exc:
        logger.error("hn_hiring_error", error=str(exc))
    return jobs


def _parse_hn_comment(text: str, comment_id: str) -> Optional[dict]:
    """Best-effort parse of HN hiring comment."""
    import html
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = html.unescape(clean).strip()

    # First line usually has the key info
    first_line = clean.split("\n")[0][:300]

    # Must contain a pipe separator (most HN job posts use | format)
    if "|" not in first_line and "–" not in first_line:
        return None

    parts = re.split(r"\s*[\|–]\s*", first_line)
    if len(parts) < 2:
        return None

    company = parts[0].strip()[:200]
    title   = parts[1].strip()[:300] if len(parts) > 1 else "Software Engineer"

    if not company or len(company) > 150:
        return None

    # Detect remote
    remote_type = None
    if re.search(r"\bremote\b", clean, re.I):
        remote_type = "remote"
    elif re.search(r"\bhybrid\b", clean, re.I):
        remote_type = "hybrid"

    # Detect location from parts
    location = None
    for part in parts[2:4]:
        p = part.strip()
        if p and len(p) < 100 and not re.search(r"https?://", p):
            location = p
            break

    # Detect salary
    salary_min, salary_max = _parse_salary(clean)

    # HN URL for the comment
    url = f"https://news.ycombinator.com/item?id={comment_id}"

    return {
        "title":           title,
        "company_name":    company,
        "job_url":         url,
        "description":     clean[:5000],
        "location":        location,
        "remote_type":     remote_type,
        "salary_min":      salary_min,
        "salary_max":      salary_max,
        "salary_currency": "USD",
        "source":          "hn_hiring",
        "external_id":     comment_id,
        "posted_at":       None,
    }


# ── Adzuna ────────────────────────────────────────────────────────────────────

_ADZUNA_COUNTRIES = ["us", "gb", "ca", "au", "de", "fr", "in", "sg", "nl"]
_ADZUNA_CATEGORIES = [
    "it-jobs", "engineering-jobs", "science-technology-jobs",
    "sales-jobs", "marketing-jobs", "finance-jobs",
]

async def _fetch_adzuna(client: httpx.AsyncClient) -> list[dict]:
    """
    https://api.adzuna.com/v1/api/jobs — free tier: 250 req/day.
    Register at developer.adzuna.com, set ADZUNA_APP_ID + ADZUNA_API_KEY in .env
    """
    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        logger.info("adzuna_skipped", reason="no credentials")
        return []

    jobs = []
    for country in _ADZUNA_COUNTRIES:
        for category in _ADZUNA_CATEGORIES[:3]:   # limit categories to save quota
            try:
                r = await client.get(
                    f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                    params={
                        "app_id":       ADZUNA_APP_ID,
                        "app_key":      ADZUNA_API_KEY,
                        "results_per_page": 50,
                        "category":     category,
                        "sort_by":      "date",
                        "content-type": "application/json",
                    },
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                for item in r.json().get("results", []):
                    title   = (item.get("title") or "").strip()
                    company = (item.get("company", {}) or {}).get("display_name", "").strip()
                    url     = (item.get("redirect_url") or "").strip()
                    if not title or not company or not url:
                        continue

                    loc_data = item.get("location", {}) or {}
                    location = ", ".join(loc_data.get("display_name", "").split(",")[:2])

                    sal_min = item.get("salary_min")
                    sal_max = item.get("salary_max")

                    jobs.append({
                        "title":           title,
                        "company_name":    company,
                        "job_url":         url,
                        "description":     item.get("description") or "",
                        "location":        location,
                        "country":         country.upper(),
                        "salary_min":      int(sal_min) if sal_min else None,
                        "salary_max":      int(sal_max) if sal_max else None,
                        "salary_currency": "USD" if country == "us" else "GBP" if country == "gb" else None,
                        "posted_at":       _parse_date(item.get("created")),
                        "source":          "adzuna",
                        "external_id":     str(item.get("id", "")),
                    })
            except Exception as exc:
                logger.warning("adzuna_error", country=country, category=category, error=str(exc))
    return jobs


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_or_create_company(db, name: str, source: str) -> int:
    """Return company.id, creating a placeholder row if needed."""
    result = await db.execute(
        select(Company.id).where(Company.name == name)
    )
    row = result.scalar_one_or_none()
    if row:
        return row

    # Create a lightweight company record tagged as job-board sourced
    stmt = pg_insert(Company).values(
        name=name,
        ats_type="jobboard",
        country="US",
        priority_score=40,
        scan_frequency_minutes=1440,   # scan daily
        active=True,
        notes=f"Auto-created from {source}",
    ).on_conflict_do_nothing(index_elements=["name"])
    await db.execute(stmt)
    await db.flush()

    result2 = await db.execute(select(Company.id).where(Company.name == name))
    return result2.scalar_one()


async def _upsert_jobs(jobs: list[dict], source: str) -> int:
    """Insert new jobs, skip duplicates (by job_url)."""
    if not jobs:
        return 0

    inserted = 0
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        seen_pairs_in_batch: set[tuple[int, str]] = set()
        for j in jobs:
            url = (j.get("job_url") or "").strip()
            if not url:
                continue

            # Skip if URL already exists
            exists = await db.execute(select(Job.id).where(Job.job_url == url))
            if exists.scalar_one_or_none():
                continue

            try:
                company_id = await _get_or_create_company(
                    db, j["company_name"], source
                )
            except Exception:
                continue

            # Skip if (company, external_id) already exists either in DB or
            # earlier in this batch — protects against `uq_job_company_external`.
            ext_id = (j.get("external_id") or "").strip() or None
            if ext_id:
                pair = (company_id, ext_id)
                if pair in seen_pairs_in_batch:
                    continue
                exists_pair = await db.execute(
                    select(Job.id).where(
                        Job.company_id == company_id,
                        Job.external_id == ext_id,
                    )
                )
                if exists_pair.scalar_one_or_none():
                    continue
                seen_pairs_in_batch.add(pair)

            # Compute freshness
            posted_at = j.get("posted_at")
            freshness_score, freshness_label = _compute_freshness(posted_at, now)

            db.add(Job(
                company_id        = company_id,
                external_id       = ext_id,
                title             = j["title"][:500],
                company_name      = j["company_name"][:500],
                description       = (j.get("description") or "")[:50000],
                location          = (j.get("location") or "")[:500] or None,
                country           = (j.get("country") or "")[:100] or None,
                remote_type       = j.get("remote_type"),
                job_url           = url[:2000],
                employment_type   = (j.get("employment_type") or "")[:100] or None,
                experience_level  = j.get("experience_level"),
                role_category     = j.get("role_category"),
                salary_min        = j.get("salary_min"),
                salary_max        = j.get("salary_max"),
                salary_currency   = j.get("salary_currency"),
                required_skills   = j.get("required_skills") or [],
                posted_at         = posted_at,
                first_seen_at     = now,
                last_seen_at      = now,
                freshness_score   = freshness_score,
                freshness_label   = freshness_label,
                source            = source,
                source_type       = "jobboard",
                source_confidence = 0.9,
                active            = True,
                fingerprint       = _fingerprint(j["title"], j["company_name"]),
            ))
            inserted += 1

            # Commit in batches of 50 to limit lost-on-error scope, and so a
            # single integrity-violation rollback only loses ~50 rows.
            if inserted % 50 == 0:
                try:
                    await db.commit()
                except Exception as exc:
                    logger.error("batch_commit_error", error=str(exc))
                    await db.rollback()
                    inserted -= 50  # uncommitted rows are gone

        try:
            await db.commit()
        except Exception as exc:
            logger.error("final_commit_error", source=source, error=str(exc))
            await db.rollback()

    return inserted


# ── Utility helpers ───────────────────────────────────────────────────────────

def _parse_date(val) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(val, str):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(val[:19], fmt[:len(val[:19])])
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _parse_salary(text: str):
    """Extract salary range from free text. Returns (min, max) in USD/year."""
    if not text:
        return None, None
    # Match patterns like $120k-$180k, $120,000-$180,000, 120k-180k
    m = re.search(
        r'\$?\s*(\d{2,3})[kK]?\s*[-–to]+\s*\$?\s*(\d{2,3})[kK]?',
        text
    )
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        # Normalize to annual
        if lo < 500:  lo *= 1000
        if hi < 500:  hi *= 1000
        if lo < hi and lo > 20000:
            return lo, hi
    return None, None


def _compute_freshness(posted_at: Optional[datetime], now: datetime):
    if not posted_at:
        return 0.5, None
    # Defensive: if a source returns a naive datetime, treat it as UTC so we
    # don't crash on tz-mismatched subtraction.
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    diff_hours = (now - posted_at).total_seconds() / 3600
    if diff_hours <= 24:
        return 1.0, "last_24h"
    if diff_hours <= 168:
        return 0.8, "last_7_days"
    if diff_hours <= 720:
        return 0.5, "last_30_days"
    return 0.2, "older"


def _fingerprint(title: str, company: str) -> str:
    key = f"{title.lower().strip()}::{company.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:64]
