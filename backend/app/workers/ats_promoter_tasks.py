"""
ats=unknown auto-promoter.

Scans companies with ats='unknown', fetches their careers page + homepage,
looks for embedded ATS markers (iframes, script tags, redirect links), and
auto-promotes them to a known ATS type so the existing scan pipeline can
pick them up.

Patterns we detect:
  • Greenhouse:    boards.greenhouse.io/embed?for=X      or  /<slug>
                   job-boards.greenhouse.io/<slug>
  • Lever:         jobs.lever.co/<slug>                 or  api.lever.co/v0/postings/<slug>
  • Ashby:         jobs.ashbyhq.com/<slug>              or  jobs.ashbyhq.com/embed?org=<slug>
  • Workable:      apply.workable.com/<slug>            or  <slug>.workable.com
  • Workday:       <co>.wd*.myworkdayjobs.com/<slug>
  • SmartRecruiters: jobs.smartrecruiters.com/<slug>
  • iCIMS:         careers-<slug>.icims.com             or  <slug>.icims.com
  • BambooHR:      <slug>.bamboohr.com
  • Teamtailor:    <slug>.teamtailor.com
  • Jobvite:       jobs.jobvite.com/<slug>
  • Recruitee:     <slug>.recruitee.com
  • Personio:      <slug>.personio.de

Runs every 30 min on Celery beat.  Cap of 50 companies per run to keep latency low.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Optional
from urllib.parse import urlparse

import asyncpg
import httpx
import structlog

from app.workers.celery_app import celery_app
from app.workers.jobboard_tasks import _run_async

logger = structlog.get_logger(__name__)

sys.path.insert(0, "/app/scripts")
try:
    from discovery_lib import detect_ats  # type: ignore
    _DETECT_AVAILABLE = True
except Exception:
    _DETECT_AVAILABLE = False

_DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# Regex that catches ANY known ATS URL embedded inside HTML.
_ATS_URL_RE = re.compile(
    r"https?://[^\"'\s<>]+(?:"
    r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|"
    r"smartrecruiters\.com|myworkdayjobs\.com|icims\.com|"
    r"teamtailor\.com|bamboohr\.com|jobvite\.com|recruitee\.com|personio\.de"
    r")[^\"'\s<>]*",
    re.I,
)


async def _probe_company(client: httpx.AsyncClient, careers_url: str) -> Optional[str]:
    """
    Fetch a company's careers/jobs page (and homepage as fallback) and
    return the first embedded ATS URL we find, else None.
    """
    if not careers_url:
        return None
    try:
        parsed = urlparse(careers_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None
    if not base:
        return None

    candidate_urls = [careers_url]
    # Also try canonical careers paths if the original URL is a homepage
    if parsed.path in ("", "/"):
        candidate_urls.extend(
            f"{base}{p}" for p in ("/careers", "/jobs", "/about/careers", "/work-with-us")
        )
    candidate_urls.append(base)  # homepage as last resort

    seen: set[str] = set()
    for url in candidate_urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            r = await client.get(url, follow_redirects=True, timeout=15.0)
            if r.status_code != 200:
                continue
            text = r.text[:100_000]
        except Exception:
            continue

        m = _ATS_URL_RE.search(text)
        if m:
            return m.group(0)
    return None


async def _run_promoter(limit: int = 50) -> dict:
    """
    Pick up to `limit` ats=unknown companies (prefer recent additions),
    probe each, and UPDATE their ats/slug if we can resolve them.
    """
    if not _DETECT_AVAILABLE:
        return {"error": "discovery_lib unavailable"}

    conn = await asyncpg.connect(_DB_DSN)
    try:
        rows = await conn.fetch(
            """
            SELECT id, name, careers_url
            FROM companies
            WHERE ats = 'unknown'
              AND careers_url IS NOT NULL
              AND careers_url != ''
            ORDER BY created_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    except Exception as e:
        await conn.close()
        logger.exception("promoter_fetch_failed", err=str(e))
        return {"error": str(e)}

    total = len(rows)
    if total == 0:
        await conn.close()
        return {"checked": 0, "promoted": 0}

    promoted = 0
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        # Probe in batches of 8 to keep concurrency reasonable
        for i in range(0, len(rows), 8):
            batch = rows[i:i+8]
            results = await asyncio.gather(
                *[_probe_company(client, r["careers_url"]) for r in batch],
                return_exceptions=True,
            )
            for row, ats_url in zip(batch, results):
                if not isinstance(ats_url, str) or not ats_url:
                    continue
                match = detect_ats(ats_url)
                if not match:
                    continue
                ats_type, slug = match
                # Don't overwrite a manually-corrected row if someone already set it
                try:
                    res = await conn.execute(
                        """
                        UPDATE companies
                        SET ats = $1, ats_identifier = $2, careers_url = $3,
                            next_scan_at = NOW() + interval '5 minutes'
                        WHERE id = $4 AND ats = 'unknown'
                        """,
                        ats_type, slug[:500], ats_url[:5000], row["id"],
                    )
                    if res and res.startswith("UPDATE 1"):
                        promoted += 1
                        logger.info(
                            "promoted_company",
                            id=row["id"], name=row["name"][:60],
                            ats=ats_type, slug=slug,
                        )
                except Exception as e:
                    logger.debug("promote_update_failed", id=row["id"], err=str(e))
            await asyncio.sleep(0.5)  # politeness between batches

    await conn.close()
    logger.info("promoter_run_done", checked=total, promoted=promoted)
    return {"checked": total, "promoted": promoted}


@celery_app.task(
    name="app.workers.ats_promoter_tasks.promote_unknown_companies",
    soft_time_limit=600, max_retries=1,
)
def promote_unknown_companies(limit: int = 50) -> dict:
    return _run_async(_run_promoter(limit=limit))
