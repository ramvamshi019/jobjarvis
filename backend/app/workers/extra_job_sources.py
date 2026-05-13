"""
Additional free job/company sources.  Each fetcher returns either:
  • a list of job dicts (goes through _upsert_jobs)            → "jobs"  flow
  • a list of (name, careers_url) tuples (goes through detect_ats + upsert_company) → "companies" flow

Sources:
  • USAJobs.gov          — official US gov API, ~5k tech jobs/day              (jobs)
  • HN Show HN / Launches — every launched startup is hiring                    (companies)
  • GitHub Trending      — owners of trending repos are almost always hiring   (companies)
  • Reddit r/forhire     — freelance + FT tech listings via JSON               (jobs)

Scheduled by celery_app.py beat_schedule.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import asyncpg
import httpx
import structlog

from app.workers.celery_app import celery_app
from app.workers.jobboard_tasks import _upsert_jobs, _run_async

logger = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Add scripts dir to path so we can use the shared discovery_lib (detect_ats, upsert_company)
sys.path.insert(0, "/app/scripts")
try:
    from discovery_lib import detect_ats, upsert_company  # type: ignore
    _DISCOVERY_AVAILABLE = True
except Exception:
    _DISCOVERY_AVAILABLE = False

_DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


# ════════════════════════════════════════════════════════════════════════════
#  1. USAJobs.gov — US Government tech jobs (BIG volume)
# ════════════════════════════════════════════════════════════════════════════
#
# https://developer.usajobs.gov/API-Reference
# Free, no key required, but they ask for a User-Agent header with your email.
# Tech roles live under JobSeriesCode 2210 (Information Technology Management).

USAJOBS_URL = "https://data.usajobs.gov/api/search"
# IMPORTANT: USAJobs requires the User-Agent header to be the email address
# you registered with at developer.usajobs.gov.  Set USAJOBS_USER_EMAIL in
# .env to match.  If unset, falls back to ramvamshikrishna0@gmail.com.
USAJOBS_HEADERS = {
    "Host": "data.usajobs.gov",
    "User-Agent": os.environ.get("USAJOBS_USER_EMAIL", "ramvamshikrishna0@gmail.com"),
    "Authorization-Key": os.environ.get("USAJOBS_API_KEY", ""),
}

# Job series codes for tech roles
USAJOBS_TECH_SERIES = [
    "2210",   # Information Technology Management
    "1550",   # Computer Science
    "0854",   # Computer Engineering
    "1530",   # Statistics (includes data science)
    "1515",   # Operations Research
]


async def fetch_usajobs(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    for series in USAJOBS_TECH_SERIES:
        for page in range(1, 11):  # up to 10 pages × 25 = 250 per series
            try:
                r = await client.get(
                    USAJOBS_URL,
                    headers=USAJOBS_HEADERS,
                    params={
                        "JobCategoryCode": series,
                        "ResultsPerPage": "25",
                        "Page": str(page),
                        "SortField": "OpenDate",
                        "SortDirection": "Desc",
                    },
                )
                if r.status_code != 200:
                    logger.warning("usajobs_status", code=r.status_code, series=series, page=page)
                    break
                data = r.json()
            except Exception as e:
                logger.warning("usajobs_failed", series=series, page=page, err=str(e))
                break

            hits = (data.get("SearchResult") or {}).get("SearchResultItems") or []
            if not hits:
                break

            for h in hits:
                desc = h.get("MatchedObjectDescriptor") or {}
                title = (desc.get("PositionTitle") or "").strip()
                agency = (desc.get("OrganizationName") or "U.S. Government").strip()
                url = desc.get("PositionURI") or ""
                # MatchedObjectId is the globally unique ID across USAJobs;
                # PositionID is just a per-agency vacancy number and collides.
                ext_id = str(h.get("MatchedObjectId") or desc.get("PositionID") or "")
                if not title or not url:
                    continue

                # Locations
                locs = desc.get("PositionLocation") or []
                loc_str = ""
                if isinstance(locs, list) and locs:
                    loc_str = locs[0].get("LocationName", "") if isinstance(locs[0], dict) else str(locs[0])

                # Posted date — USAJobs returns naive ISO strings; force UTC.
                posted_at = None
                pd = desc.get("PublicationStartDate") or desc.get("PositionStartDate")
                try:
                    if pd:
                        dt = datetime.fromisoformat(pd.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        posted_at = dt
                except Exception:
                    pass

                # Salary
                salary_min = salary_max = None
                pay_range = desc.get("PositionRemuneration") or []
                if isinstance(pay_range, list) and pay_range:
                    try:
                        salary_min = float(pay_range[0].get("MinimumRange") or 0) or None
                        salary_max = float(pay_range[0].get("MaximumRange") or 0) or None
                    except Exception:
                        pass

                out.append({
                    "external_id":    ext_id,
                    "title":          title,
                    "company_name":   agency,
                    "description":    (desc.get("QualificationSummary") or "")[:5000],
                    "location":       loc_str[:500],
                    "country":        "US",
                    "job_url":        url,
                    "employment_type": "Full-Time",
                    "salary_min":     salary_min,
                    "salary_max":     salary_max,
                    "salary_currency": "USD",
                    "posted_at":      posted_at,
                })

            if len(hits) < 25:
                break

    logger.info("usajobs_total_fetched", count=len(out))
    return out


# ════════════════════════════════════════════════════════════════════════════
#  2. Hacker News "Show HN" / "Launch HN" — discover new hiring startups
# ════════════════════════════════════════════════════════════════════════════
#
# Algolia search API for HN.  Show HN and Launch HN posts always include a
# company URL.  We then probe that URL for /careers, /jobs paths.

HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"


async def discover_hn_launches(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Return list of (company_name, careers_url) candidates from HN."""
    companies: dict[str, str] = {}

    for query in ["Show HN", "Launch HN"]:
        try:
            r = await client.get(
                HN_ALGOLIA_URL,
                params={
                    "query": query,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())}",
                    "hitsPerPage": "100",
                },
            )
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception as e:
            logger.warning("hn_launches_failed", query=query, err=str(e))
            continue

        for hit in data.get("hits") or []:
            title = (hit.get("title") or "").strip()
            url = (hit.get("url") or "").strip()
            if not url or not title:
                continue
            # Extract company name from the title — usually "Show HN: <Product> – <desc>"
            name = title.replace("Show HN:", "").replace("Launch HN:", "").strip()
            name = re.split(r"\s+[-–—]\s+", name)[0].strip()[:200]
            if not name:
                continue
            companies[urlparse(url).netloc.lower()] = url

    # For each domain, probe common careers paths and detect ATS
    async def _probe_careers(domain: str, base_url: str) -> Optional[tuple[str, str]]:
        for path in ["/careers", "/jobs", "/about/careers", "/company/careers"]:
            try:
                careers_url = f"https://{domain}{path}"
                r = await client.get(careers_url, timeout=10.0)
                if r.status_code == 200:
                    # Look for known ATS links in the page
                    text = r.text[:50000]
                    for ats_host in [
                        "greenhouse.io", "lever.co", "ashbyhq.com",
                        "workable.com", "smartrecruiters.com",
                        "myworkdayjobs.com", "icims.com", "teamtailor.com",
                    ]:
                        m = re.search(
                            r"https?://[^\"'\s]+" + re.escape(ats_host) + r"[^\"'\s]*",
                            text, re.I,
                        )
                        if m:
                            return (domain.replace(".com", "").replace(".io", "").title(), m.group(0))
                    # No ATS found; return the careers URL itself as fallback
                    return (domain.replace(".com", "").replace(".io", "").title(), careers_url)
            except Exception:
                continue
        return None

    results: list[tuple[str, str]] = []
    # Probe in parallel batches of 8
    domains = list(companies.items())[:60]   # cap at 60 to keep run-time reasonable
    for i in range(0, len(domains), 8):
        batch = domains[i:i + 8]
        outs = await asyncio.gather(
            *[_probe_careers(d, u) for d, u in batch], return_exceptions=True,
        )
        for o in outs:
            if isinstance(o, tuple):
                results.append(o)

    logger.info("hn_launches_companies_found", count=len(results))
    return results


# ════════════════════════════════════════════════════════════════════════════
#  3. GitHub Trending repos — owners of trending repos hire heavily
# ════════════════════════════════════════════════════════════════════════════

GITHUB_TRENDING_URL = "https://github.com/trending"


async def discover_github_trending(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Scrape GitHub Trending (HTML), get repo owner orgs, look up their careers."""
    companies: dict[str, str] = {}

    for since in ["daily", "weekly", "monthly"]:
        try:
            r = await client.get(
                f"{GITHUB_TRENDING_URL}?since={since}",
                timeout=20.0,
            )
            if r.status_code != 200:
                continue
            html = r.text
        except Exception as e:
            logger.warning("github_trending_failed", since=since, err=str(e))
            continue

        # Repo links: <h2 class="..."><a href="/OWNER/REPO" ...>
        for m in re.finditer(r'<h2[^>]*><a href="/([^/"]+)/([^"]+)"', html):
            owner = m.group(1)
            repo = m.group(2)
            if owner in companies:
                continue
            companies[owner] = repo

    # For each org, fetch the org page and try to find its careers/website link
    async def _resolve_org(owner: str, repo: str) -> Optional[tuple[str, str]]:
        try:
            r = await client.get(
                f"https://api.github.com/orgs/{owner}",
                timeout=10.0,
                headers={"Accept": "application/vnd.github+json"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception:
            return None

        name = data.get("name") or owner
        blog = data.get("blog") or ""
        if not blog:
            return None
        if not blog.startswith("http"):
            blog = "https://" + blog

        # Probe for careers/jobs page on their site
        domain = urlparse(blog).netloc
        for path in ["/careers", "/jobs", "/about/careers"]:
            try:
                rr = await client.get(f"https://{domain}{path}", timeout=8.0)
                if rr.status_code == 200:
                    return (name[:200], f"https://{domain}{path}")
            except Exception:
                continue
        # Fallback: just save the blog/website as careers_url
        return (name[:200], blog)

    results: list[tuple[str, str]] = []
    items = list(companies.items())[:50]
    for i in range(0, len(items), 6):
        batch = items[i:i + 6]
        outs = await asyncio.gather(
            *[_resolve_org(o, r) for o, r in batch], return_exceptions=True,
        )
        for o in outs:
            if isinstance(o, tuple):
                results.append(o)
        await asyncio.sleep(1.0)   # GitHub API politeness

    logger.info("github_trending_companies_found", count=len(results))
    return results


# ════════════════════════════════════════════════════════════════════════════
#  4. Reddit r/forhire & r/cscareerquestions  — niche tech listings (JSON)
# ════════════════════════════════════════════════════════════════════════════

REDDIT_SUBS = [
    "forhire",
    "remotejs",
    "techjobsforuk",   # global
]

REDDIT_HEADERS = {
    "User-Agent": "JobJarvis/1.0 (by ramvamshikrishna0@gmail.com)",
}


async def fetch_reddit_hiring(client: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    for sub in REDDIT_SUBS:
        try:
            r = await client.get(
                f"https://www.reddit.com/r/{sub}/new.json",
                headers=REDDIT_HEADERS,
                params={"limit": "100"},
                timeout=20.0,
            )
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception as e:
            logger.warning("reddit_failed", sub=sub, err=str(e))
            continue

        for child in (data.get("data") or {}).get("children") or []:
            post = child.get("data") or {}
            title = (post.get("title") or "").strip()
            # Filter for hiring posts only (skip [FOR HIRE] self-promotion)
            t_up = title.upper()
            if not any(tag in t_up for tag in ["[HIRING]", "[H]"]):
                continue

            permalink = post.get("permalink") or ""
            url = f"https://www.reddit.com{permalink}" if permalink else ""
            if not url:
                continue

            posted_at = None
            try:
                ts = post.get("created_utc")
                if ts:
                    posted_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                    if posted_at.tzinfo is None:
                        posted_at = posted_at.replace(tzinfo=timezone.utc)
            except Exception:
                pass

            # Clean title for use as job title
            clean_title = re.sub(r"\[(HIRING|H|FOR HIRE)\]", "", title, flags=re.I).strip()

            out.append({
                "external_id":   post.get("id") or "",
                "title":         clean_title[:500],
                "company_name":  "Reddit / r/" + sub,
                "description":   (post.get("selftext") or "")[:5000],
                "location":      "Remote",
                "country":       None,
                "remote_type":   "remote",
                "job_url":       url,
                "posted_at":     posted_at,
            })

    logger.info("reddit_total_fetched", count=len(out))
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Company-discovery upsert helper (for HN + GitHub Trending sources)
# ════════════════════════════════════════════════════════════════════════════

async def _upsert_discovered_companies(pairs: list[tuple[str, str]], source: str) -> int:
    if not pairs or not _DISCOVERY_AVAILABLE:
        return 0
    conn = await asyncpg.connect(_DB_DSN)
    inserted = 0
    for name, url in pairs:
        if not name or not url:
            continue
        match = detect_ats(url)
        if match:
            ats_type, slug = match
        else:
            ats_type = "unknown"
            slug = name.lower().replace(" ", "-")[:60]
        try:
            cid = await upsert_company(
                conn, name=name, ats=ats_type, slug=slug, careers_url=url,
            )
            if cid:
                inserted += 1
        except Exception as e:
            logger.debug("upsert_failed", name=name, err=str(e))
    await conn.close()
    logger.info("companies_upserted", source=source, inserted=inserted)
    return inserted


# ════════════════════════════════════════════════════════════════════════════
#  Master tasks
# ════════════════════════════════════════════════════════════════════════════

async def _run_usajobs() -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        jobs = await fetch_usajobs(client)
    inserted = await _upsert_jobs(jobs, source="usajobs")
    return {"fetched": len(jobs), "inserted": inserted}


async def _run_reddit() -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        jobs = await fetch_reddit_hiring(client)
    inserted = await _upsert_jobs(jobs, source="reddit")
    return {"fetched": len(jobs), "inserted": inserted}


async def _run_hn_launches() -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        pairs = await discover_hn_launches(client)
    inserted = await _upsert_discovered_companies(pairs, source="hn_launches")
    return {"discovered": len(pairs), "inserted": inserted}


async def _run_github_trending() -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        pairs = await discover_github_trending(client)
    inserted = await _upsert_discovered_companies(pairs, source="github_trending")
    return {"discovered": len(pairs), "inserted": inserted}


@celery_app.task(
    name="app.workers.extra_job_sources.fetch_usajobs",
    soft_time_limit=1200, max_retries=1,
)
def task_fetch_usajobs(): return _run_async(_run_usajobs())

@celery_app.task(
    name="app.workers.extra_job_sources.fetch_reddit",
    soft_time_limit=600, max_retries=1,
)
def task_fetch_reddit(): return _run_async(_run_reddit())

@celery_app.task(
    name="app.workers.extra_job_sources.discover_hn_launches",
    soft_time_limit=900, max_retries=1,
)
def task_discover_hn_launches(): return _run_async(_run_hn_launches())

@celery_app.task(
    name="app.workers.extra_job_sources.discover_github_trending",
    soft_time_limit=900, max_retries=1,
)
def task_discover_github_trending(): return _run_async(_run_github_trending())


@celery_app.task(
    name="app.workers.extra_job_sources.fetch_all_extra_sources",
    soft_time_limit=3600, max_retries=1,
)
def fetch_all_extra_sources() -> dict:
    async def _all():
        results = {}
        for name, runner in [
            ("usajobs",          _run_usajobs),
            ("reddit",           _run_reddit),
            ("hn_launches",      _run_hn_launches),
            ("github_trending",  _run_github_trending),
        ]:
            try:
                results[name] = await runner()
            except Exception as e:
                logger.exception("extra_source_fail", source=name, err=str(e))
                results[name] = {"error": str(e)}
        return results
    return _run_async(_all())
