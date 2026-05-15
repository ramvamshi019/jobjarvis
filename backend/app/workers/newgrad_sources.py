"""
New-grad / entry-level job sources.

Three curated GitHub repos host markdown-rendered HTML tables of new-grad
and internship positions that get updated daily by a small army of
contributors.  They're the canonical lists for new-grad SWE hiring:

  • SimplifyJobs/New-Grad-Positions    — ~300 active full-time new-grad roles
  • SimplifyJobs/Summer2026-Internships — ~800 active US tech internships
  • vanshb03/New-Grad-2025              — community-maintained alt list

Each task pulls the README, parses every `<tr>` row, and upserts the
resulting jobs via `_upsert_jobs`.  Jobs land with `experience_level=entry`
so they automatically rank higher for new-grad users.  Companies that
aren't already in the corpus get auto-created (`_get_or_create_company`).

This is the highest-signal source for the target user — every row is
explicitly tagged "entry level" by the contributors, not buried in a
"3+ YOE" listing.  Free.  Refreshed daily.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import structlog
from bs4 import BeautifulSoup

from app.workers.celery_app import celery_app
from app.workers.jobboard_tasks import _upsert_jobs, _run_async

logger = structlog.get_logger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobJarvis/1.0 +https://jobjar.duckdns.org)",
    "Accept":     "text/markdown,text/html,*/*",
}
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ── Source configuration ─────────────────────────────────────────────────────

_NEWGRAD_SOURCES: list[tuple[str, str]] = [
    ("simplify_newgrad",
     "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"),
    ("simplify_summer2026",
     "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"),
    ("vanshb03_newgrad2025",
     "https://raw.githubusercontent.com/vanshb03/New-Grad-2025/main/README.md"),
]


# ── HTML parsing ─────────────────────────────────────────────────────────────

# These repos render the job tables as raw HTML inside the markdown so each
# row is on its own block:
#   <tr>
#     <td><strong><a href="https://simplify.jobs/c/<slug>?...">Company Name</a></strong></td>
#     <td>Role Title</td>
#     <td>Location</td>
#     <td>... <a href="https://<real-ATS-URL>?utm_source=Simplify&ref=Simplify">Apply</a> ...</td>
#     <td>2d</td>
#   </tr>
#
# The "↳" character in the first <td> marks a sub-row (same company, multiple
# roles).  We thread the last seen company name across these.

_SIMPLIFY_UTM_PARAM = re.compile(r"[?&](?:utm_source|ref|gh_jid|gh_src)=[^&]*", re.I)

# Locations the contributors use to mark "remote" or "n/a"
_REMOTE_RE = re.compile(r"\bremote\b", re.I)

# Date strings: "0d", "1d", "13d", "Posted 5 days ago", "Yesterday"
_AGE_RE = re.compile(r"^(\d+)\s*d", re.I)


def _strip_utm(url: str) -> str:
    """Strip Simplify's tracking parameters from the apply URL so we don't
    accumulate duplicate variants of the same job."""
    if not url:
        return ""
    # Two passes (some URLs have ? then & with multiple tracking params)
    return _SIMPLIFY_UTM_PARAM.sub("", url).rstrip("?&")


def _parse_age(age_text: str, now: datetime) -> Optional[datetime]:
    if not age_text:
        return None
    m = _AGE_RE.search(age_text.strip())
    if m:
        try:
            return now - timedelta(days=int(m.group(1)))
        except ValueError:
            return None
    return None


def _parse_readme(html: str, source_label: str) -> list[dict]:
    """Extract one job dict per <tr> row.  Returns a list ready for
    `_upsert_jobs()`."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    last_company = ""    # threaded across continuation rows
    now = datetime.now(timezone.utc)

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        # td[0] = company; sometimes "↳" for continuation
        co_cell = tds[0]
        co_text = co_cell.get_text(strip=True)
        if co_text and co_text != "↳":
            last_company = co_text
        company = last_company.strip()
        if not company:
            continue

        # td[1] = role title
        title = tds[1].get_text(" ", strip=True)
        if not title or len(title) > 250:
            continue

        # td[2] = location
        loc_text = tds[2].get_text(" / ", strip=True)
        remote = "remote" if _REMOTE_RE.search(loc_text) else None

        # td[3] = apply links; pick the FIRST real ATS link (not simplify.jobs)
        apply_url = ""
        for a in tds[3].find_all("a"):
            href = a.get("href") or ""
            if not href:
                continue
            if "simplify.jobs" in href:
                # Simplify's redirector — keep as fallback but prefer the
                # direct ATS link if there is one.
                if not apply_url:
                    apply_url = href
                continue
            apply_url = href
            break
        apply_url = _strip_utm(apply_url)
        if not apply_url:
            continue

        # td[4] = "0d"/"5d" age string; td[-1] is the same on these tables
        posted_at = None
        if len(tds) >= 5:
            posted_at = _parse_age(tds[4].get_text(" ", strip=True), now)
        if posted_at is None:
            posted_at = _parse_age(tds[-1].get_text(" ", strip=True), now)

        # Skip closed roles — the contributors mark them 🔒 in the title
        if "🔒" in title or "closed" in title.lower():
            continue

        out.append({
            "external_id":      apply_url[:200],            # stable id
            "title":            title[:500],
            "company_name":     company[:500],
            "description":      f"New-grad role from {source_label}",
            "location":         loc_text[:500] or None,
            "country":          "US",
            "remote_type":      remote,
            "job_url":          apply_url[:2000],
            "employment_type":  "Full-Time",
            "experience_level": "entry",                    # the whole point of these lists
            "role_category":    None,                       # downstream classifier handles it
            "posted_at":        posted_at,
            "source":           source_label,
        })

    return out


# ── Per-source fetcher ───────────────────────────────────────────────────────

async def _fetch_and_upsert(label: str, url: str) -> dict:
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code != 200:
            logger.warning("newgrad_fetch_failed", source=label, status=r.status_code)
            return {"source": label, "fetched": 0, "inserted": 0, "error": f"http {r.status_code}"}
        rows = _parse_readme(r.text, label)
    except Exception as e:
        logger.exception("newgrad_parse_failed", source=label, err=str(e))
        return {"source": label, "fetched": 0, "inserted": 0, "error": str(e)[:120]}

    if not rows:
        logger.info("newgrad_no_rows", source=label)
        return {"source": label, "fetched": 0, "inserted": 0}

    inserted = await _upsert_jobs(rows, source=label)
    logger.info("newgrad_done", source=label, fetched=len(rows), inserted=inserted)
    return {"source": label, "fetched": len(rows), "inserted": inserted}


# ── Celery tasks ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="app.workers.newgrad_sources.fetch_simplify_newgrad",
    soft_time_limit=300, max_retries=1,
)
def fetch_simplify_newgrad() -> dict:
    """SimplifyJobs/New-Grad-Positions — ~300 active full-time new-grad SWE roles."""
    return _run_async(_fetch_and_upsert(*_NEWGRAD_SOURCES[0]))


@celery_app.task(
    name="app.workers.newgrad_sources.fetch_simplify_summer2026",
    soft_time_limit=600, max_retries=1,
)
def fetch_simplify_summer2026() -> dict:
    """SimplifyJobs/Summer2026-Internships — ~800 US tech internships."""
    return _run_async(_fetch_and_upsert(*_NEWGRAD_SOURCES[1]))


@celery_app.task(
    name="app.workers.newgrad_sources.fetch_vanshb03_newgrad",
    soft_time_limit=300, max_retries=1,
)
def fetch_vanshb03_newgrad() -> dict:
    """vanshb03/New-Grad-2025 — community-maintained new-grad list."""
    return _run_async(_fetch_and_upsert(*_NEWGRAD_SOURCES[2]))


@celery_app.task(
    name="app.workers.newgrad_sources.fetch_all_newgrad",
    soft_time_limit=1800, max_retries=0,
)
def fetch_all_newgrad() -> dict:
    """Fire every new-grad source sequentially."""
    async def _go():
        results = []
        for label, url in _NEWGRAD_SOURCES:
            r = await _fetch_and_upsert(label, url)
            results.append(r)
        total = sum(r.get("inserted", 0) for r in results)
        logger.info("newgrad_all_done", total=total, per_source=results)
        return {"per_source": results, "total_inserted": total}
    return _run_async(_go())
