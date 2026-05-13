"""
Direct fetchers for big-volume US tech employers.

These companies don't sit behind a standard ATS we can detect by URL —
they run their own careers sites with public JSON APIs.  Hitting these
directly every hour adds hundreds of fresh jobs per day per employer that
wouldn't otherwise reach our index.

Sources (all public, no auth):
  • Amazon       : amazon.jobs/en/search.json
  • Microsoft    : gcsservices.careers.microsoft.com search API
  • Apple        : jobs.apple.com/api/role/search
  • Google       : Google careers search (JSON via the `_data` route)
  • Stripe       : stripe.com/jobs/listing.json     (bonus — single endpoint, all jobs)

Each fetcher returns a list of standardized job dicts that go through
the same `_upsert_jobs()` helper as the other job-board sources.

Scheduled hourly by celery beat (see celery_app.py).
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone

import httpx
import structlog

from app.workers.celery_app import celery_app
from app.workers.jobboard_tasks import _upsert_jobs, _run_async

logger = structlog.get_logger(__name__)

_HEADERS = {
    "User-Agent": "JobJarvis/1.0 ramvamshikrishna0@gmail.com",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ───────────────────────────────────────────────────────────────────────────
#  Amazon — ~500 US tech jobs added per day
# ───────────────────────────────────────────────────────────────────────────

AMAZON_URL = "https://www.amazon.jobs/en/search.json"
AMAZON_TECH_CATEGORIES = [
    "software-development",
    "systems-quality-and-security-engineering",
    "hardware-development",
    "data-science",
    "machine-learning-science",
    "research-science",
    "solutions-architect",
]


async def fetch_amazon(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    for cat in AMAZON_TECH_CATEGORIES:
        for offset in range(0, 500, 100):  # cap at 500 per category per run
            params = [
                ("normalized_country_code[]", "US"),
                ("radius", "24km"),
                ("result_limit", "100"),
                ("sort", "recent"),
                ("category[]", cat),
                ("offset", str(offset)),
            ]
            try:
                r = await client.get(AMAZON_URL, params=params)
                if r.status_code != 200:
                    break
                data = r.json()
            except Exception as e:
                logger.warning("amazon_fetch_failed", cat=cat, offset=offset, err=str(e))
                break

            jobs = data.get("jobs") or []
            if not jobs:
                break

            for j in jobs:
                title = (j.get("title") or "").strip()
                path = j.get("job_path") or ""
                if not title or not path:
                    continue
                url = f"https://www.amazon.jobs{path}" if path.startswith("/") else path

                # Parse posted date (Amazon returns "Posted 3 days ago" or ISO)
                posted_at = None
                pd = j.get("posted_date") or j.get("updated_time")
                if pd:
                    try:
                        posted_at = datetime.fromisoformat(pd.replace("Z", "+00:00"))
                    except Exception:
                        posted_at = None

                out.append({
                    "external_id":   str(j.get("id_icims") or j.get("id") or ""),
                    "title":         title,
                    "company_name":  "Amazon",
                    "description":   (j.get("description_short") or j.get("description") or "")[:5000],
                    "location":      j.get("normalized_location") or j.get("location") or "",
                    "country":       "US",
                    "remote_type":   None,
                    "job_url":       url,
                    "employment_type": j.get("schedule_type_help") or "Full-Time",
                    "posted_at":     posted_at,
                })

            if len(jobs) < 100:
                break

    logger.info("amazon_fetched", count=len(out))
    return out


# ───────────────────────────────────────────────────────────────────────────
#  Microsoft — ~200 US tech jobs added per day
# ───────────────────────────────────────────────────────────────────────────

MICROSOFT_URL = "https://gcsservices.careers.microsoft.com/search/api/v1/search"
MICROSOFT_PROFESSIONS = [
    "Software Engineering",
    "Hardware Engineering",
    "Data Science",
    "Research, Applied, & Data Sciences",
    "Engineering",
]


async def fetch_microsoft(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    for prof in MICROSOFT_PROFESSIONS:
        for page in range(1, 6):  # 5 pages × 50 = up to 250 per profession
            try:
                r = await client.get(
                    MICROSOFT_URL,
                    params={
                        "p":  prof,
                        "lc": "United States",
                        "l":  "en_us",
                        "pgSz": "50",
                        "pg":  str(page),
                        "o":   "Recent",
                    },
                )
                if r.status_code != 200:
                    break
                data = r.json()
            except Exception as e:
                logger.warning("microsoft_fetch_failed", prof=prof, page=page, err=str(e))
                break

            jobs = (((data.get("operationResult") or {}).get("result") or {}).get("jobs") or [])
            if not jobs:
                break

            for j in jobs:
                job_id = str(j.get("jobId") or "")
                title  = (j.get("title") or "").strip()
                if not job_id or not title:
                    continue

                pd = j.get("postingDate") or ""
                posted_at = None
                try:
                    posted_at = datetime.fromisoformat(pd.replace("Z", "+00:00"))
                except Exception:
                    pass

                props = j.get("properties") or {}
                locations = props.get("locations") or []
                loc_str = ", ".join(locations[:2]) if isinstance(locations, list) else str(locations)[:200]

                out.append({
                    "external_id":   job_id,
                    "title":         title,
                    "company_name":  "Microsoft",
                    "description":   (j.get("jobSummary") or "")[:5000],
                    "location":      loc_str,
                    "country":       "US",
                    "job_url":       f"https://jobs.careers.microsoft.com/global/en/job/{job_id}",
                    "employment_type": props.get("employmentType") or "Full-Time",
                    "posted_at":     posted_at,
                })

            if len(jobs) < 50:
                break

    logger.info("microsoft_fetched", count=len(out))
    return out


# ───────────────────────────────────────────────────────────────────────────
#  Apple — ~100 US tech jobs added per day
# ───────────────────────────────────────────────────────────────────────────

APPLE_URL = "https://jobs.apple.com/api/role/search"


async def fetch_apple(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    # Apple's API expects POST with a JSON body
    body = {
        "filters": {
            "range": {"standardWeeklyHours": {"start": 0, "end": 168}},
            "locations": ["postLocation-USA"],
        },
        "page": 1,
        "locale": "en-us",
        "search": "engineer",
        "sort": "newest",
    }
    for page in range(1, 11):  # up to 10 pages
        body["page"] = page
        try:
            r = await client.post(APPLE_URL, json=body)
            if r.status_code != 200:
                break
            data = r.json()
        except Exception as e:
            logger.warning("apple_fetch_failed", page=page, err=str(e))
            break

        roles = data.get("searchResults") or data.get("roles") or []
        if not roles:
            break

        for j in roles:
            rid = str(j.get("id") or j.get("positionId") or "")
            title = (j.get("postingTitle") or j.get("title") or "").strip()
            if not rid or not title:
                continue

            loc_list = j.get("locations") or []
            loc = ""
            if isinstance(loc_list, list) and loc_list:
                first = loc_list[0]
                loc = first.get("name") if isinstance(first, dict) else str(first)

            posted_at = None
            pd = j.get("postDateInGMT") or j.get("postingDate")
            try:
                if pd:
                    posted_at = datetime.fromisoformat(pd.replace("Z", "+00:00"))
            except Exception:
                pass

            out.append({
                "external_id":   rid,
                "title":         title,
                "company_name":  "Apple",
                "description":   (j.get("jobSummary") or "")[:5000],
                "location":      loc,
                "country":       "US",
                "job_url":       f"https://jobs.apple.com/en-us/details/{rid}",
                "posted_at":     posted_at,
            })

        if len(roles) < 20:
            break

    logger.info("apple_fetched", count=len(out))
    return out


# ───────────────────────────────────────────────────────────────────────────
#  Google — Variable, ~100 US tech jobs added per day
# ───────────────────────────────────────────────────────────────────────────

# Google's careers site is React; their internal API is at:
GOOGLE_URL = "https://www.google.com/about/careers/applications/jobs/results"


async def fetch_google(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    # Google paginates via ?page=N. We hit pages 1..5, accept whatever returns.
    for page in range(1, 6):
        try:
            r = await client.get(
                GOOGLE_URL,
                params={
                    "location": "United States",
                    "page":     str(page),
                    "q":        "software engineer",
                    "_data":    "routes/about.careers.applications.jobs.results",
                },
            )
            if r.status_code != 200:
                break
            data = r.json()
        except Exception as e:
            logger.warning("google_fetch_failed", page=page, err=str(e))
            break

        jobs = data.get("jobs") or []
        if not jobs:
            break

        for j in jobs:
            jid = str(j.get("id") or j.get("company_id") or "")
            title = (j.get("title") or "").strip()
            if not title:
                continue

            url = j.get("apply_url") or f"https://www.google.com/about/careers/applications/jobs/results/{jid}"

            loc = ""
            locs = j.get("locations") or []
            if isinstance(locs, list) and locs:
                loc = locs[0].get("display") if isinstance(locs[0], dict) else str(locs[0])

            out.append({
                "external_id":   jid,
                "title":         title,
                "company_name":  "Google",
                "description":   (j.get("description") or j.get("summary") or "")[:5000],
                "location":      loc,
                "country":       "US",
                "job_url":       url,
                "posted_at":     None,
            })

        if len(jobs) < 20:
            break

    logger.info("google_fetched", count=len(out))
    return out


# ───────────────────────────────────────────────────────────────────────────
#  Stripe — single endpoint, ALL their jobs in one call (bonus)
# ───────────────────────────────────────────────────────────────────────────

STRIPE_URL = "https://stripe.com/jobs/listing.json"


async def fetch_stripe(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    try:
        r = await client.get(STRIPE_URL)
        if r.status_code != 200:
            return out
        data = r.json()
    except Exception as e:
        logger.warning("stripe_fetch_failed", err=str(e))
        return out

    for j in (data.get("jobs") or []):
        title = (j.get("name") or "").strip()
        slug = (j.get("relativeUrl") or j.get("absoluteUrl") or "")
        if not title or not slug:
            continue
        url = slug if slug.startswith("http") else f"https://stripe.com{slug}"

        # Stripe stores office locations on the job; pick the first US one if available
        loc = ""
        offices = j.get("offices") or []
        if isinstance(offices, list) and offices:
            loc = offices[0]

        out.append({
            "external_id":   str(j.get("id") or ""),
            "title":         title,
            "company_name":  "Stripe",
            "description":   (j.get("description") or "")[:5000],
            "location":      loc,
            "country":       "US" if "US" in loc.upper() else None,
            "job_url":       url,
            "posted_at":     None,
        })

    logger.info("stripe_fetched", count=len(out))
    return out


# ───────────────────────────────────────────────────────────────────────────
#  Master entrypoint
# ───────────────────────────────────────────────────────────────────────────

async def _fetch_all_async() -> dict:
    results: dict[str, int] = {}
    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True,
    ) as client:
        for name, fn in [
            ("amazon",    fetch_amazon),
            ("microsoft", fetch_microsoft),
            ("apple",     fetch_apple),
            ("google",    fetch_google),
            ("stripe",    fetch_stripe),
        ]:
            try:
                jobs = await fn(client)
                inserted = await _upsert_jobs(jobs, source=name)
                results[name] = inserted
                logger.info("megaemployer_done", source=name, inserted=inserted, fetched=len(jobs))
            except Exception as e:
                logger.exception("megaemployer_fail", source=name, err=str(e))
                results[name] = -1
    return results


@celery_app.task(
    name="app.workers.megaemployer_tasks.fetch_all_megaemployers",
    soft_time_limit=1800,
    max_retries=1,
)
def fetch_all_megaemployers() -> dict:
    return _run_async(_fetch_all_async())
