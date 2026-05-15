"""
Scale-out maintenance tasks.

Three periodic jobs that keep storage and scan-budget healthy as the corpus
approaches 6-figure / 7-figure scale:

  1. ``prune_bronze_jobs`` — drops `bronze_raw_jobs` rows older than
     BRONZE_RETENTION_DAYS (default 7).  Each scan persists the full raw ATS
     payload to Bronze for replay/debugging; left unbounded it grows by
     several GB/day at 1M-company scale.

  2. ``decay_inactive_companies`` — deactivates companies whose
     last_job_found_at is older than COMPANY_DECAY_DAYS (default 180) and
     never get demoted by promote_active_companies (the "no jobs at all"
     case).  Keeps the active scan pool tight so each tier-scan dispatch
     covers the companies that actually post jobs.

  3. ``fix_workday_slugs`` — Workday's CXS API needs `tenant|board|shard`,
     but the generic discovery code only captures the tenant.  Fetches
     each Workday-tagged company's careers URL, parses the canonical
     `myworkdayjobs.com/<locale>/<board>` path, and rewrites
     ats_identifier so the next scan actually returns jobs.  Catches
     ~30–50 F500 / mid-cap Workday tenants (Tesla, Salesforce, Uber, Lyft,
     IBM, MongoDB, Snowflake, etc.) that otherwise return 0 jobs.

All idempotent and safe to re-run.  Each task logs row counts so the
admin / pipeline_metrics endpoints can show maintenance health.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone, timedelta

import httpx
import structlog
from sqlalchemy import text

from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

BRONZE_RETENTION_DAYS  = int(os.environ.get("BRONZE_RETENTION_DAYS", "7"))
COMPANY_DECAY_DAYS     = int(os.environ.get("COMPANY_DECAY_DAYS",    "180"))


def _run_async(coro):
    async def _wrapper():
        from app.database import async_engine
        await async_engine.dispose()
        return await coro
    return asyncio.run(_wrapper())


# ── 1. Bronze TTL ─────────────────────────────────────────────────────────────

async def _prune_bronze_async() -> dict:
    from app.database import AsyncSessionLocal
    cutoff = datetime.now(timezone.utc) - timedelta(days=BRONZE_RETENTION_DAYS)
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("DELETE FROM bronze_raw_jobs WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
        deleted = r.rowcount or 0
        await db.commit()
    logger.info(
        "bronze_pruned",
        deleted=deleted,
        cutoff=cutoff.isoformat(),
        retention_days=BRONZE_RETENTION_DAYS,
    )
    return {"deleted": deleted, "cutoff": cutoff.isoformat()}


@celery_app.task(
    name="app.workers.maintenance_tasks.prune_bronze_jobs",
    soft_time_limit=900, max_retries=1,
)
def prune_bronze_jobs() -> dict:
    """Delete bronze_raw_jobs older than BRONZE_RETENTION_DAYS (default 7)."""
    return _run_async(_prune_bronze_async())


# ── 2. Company decay ──────────────────────────────────────────────────────────

async def _decay_inactive_companies_async() -> dict:
    """Deactivate companies that have never returned a job in the last
    COMPANY_DECAY_DAYS days.  This is the downward counterpart to
    `promote_active_companies`."""
    from app.database import AsyncSessionLocal
    cutoff = datetime.now(timezone.utc) - timedelta(days=COMPANY_DECAY_DAYS)

    # Two cohorts: (a) companies that have last_job_found_at older than cutoff,
    # and (b) companies that have NEVER found a job and were created before
    # cutoff.  Both indicate a dead ATS / dead company.
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("""
                UPDATE companies
                   SET active = false,
                       updated_at = NOW()
                 WHERE active = true
                   AND (
                        (last_job_found_at IS NOT NULL AND last_job_found_at < :cutoff)
                        OR
                        (last_job_found_at IS NULL AND jobs_found_count = 0
                            AND created_at < :cutoff)
                   )
            """),
            {"cutoff": cutoff},
        )
        deactivated = r.rowcount or 0
        await db.commit()

    logger.info(
        "companies_decayed",
        deactivated=deactivated,
        cutoff=cutoff.isoformat(),
        decay_days=COMPANY_DECAY_DAYS,
    )
    return {"deactivated": deactivated, "cutoff": cutoff.isoformat()}


@celery_app.task(
    name="app.workers.maintenance_tasks.decay_inactive_companies",
    soft_time_limit=600, max_retries=1,
)
def decay_inactive_companies() -> dict:
    """Deactivate companies idle for COMPANY_DECAY_DAYS (default 180)."""
    return _run_async(_decay_inactive_companies_async())


# ── 3. Workday board-slug auto-discoverer ────────────────────────────────────

# Match the canonical Workday careers URL.  Examples:
#   https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/...
#   https://salesforce.wd1.myworkdayjobs.com/External_Career_Site
#   https://lyft.wd1.myworkdayjobs.com/LyftExternalCareerSite
# We capture (tenant, shard, locale, board) — locale is optional.
_WORKDAY_URL_RE = re.compile(
    r"https?://"
    r"([a-z0-9][\w-]+)\."          # tenant
    r"(wd[0-9]+)\."                # shard
    r"myworkdayjobs\.com"
    r"(?:/[a-z]{2}-[A-Z]{2})?"     # optional /en-US locale (skipped)
    r"/([\w-]+)",                  # board
    re.I,
)

_WORKDAY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobJarvis/1.0 +https://jobjar.duckdns.org)",
    "Accept":     "text/html,application/json,*/*",
}
_WORKDAY_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# Workday URLs sometimes contain non-board path segments — most commonly
# `robots.txt` (from `<link rel="canonical" href=".../robots.txt">` style
# anti-bot markers, or our scraper bouncing through robots).  Anything in
# this set is rejected as a board slug.
_WORKDAY_BOARD_BLACKLIST: frozenset[str] = frozenset({
    "robots", "robots.txt", "sitemap", "sitemap.xml",
    "favicon", "favicon.ico",
    "static", "assets", "_next", "_nuxt", "api",
    "wday", "en-us", "en-gb", "fr-fr", "de-de", "es-es",
    "login", "logout", "signin", "signout",
})


async def _scrape_workday_slug(client: httpx.AsyncClient, careers_url: str) -> tuple[str, str, str] | None:
    """Return (tenant, board, shard) if we can extract a complete Workday
    triple from the company's careers page; otherwise None.

    We try three lookups in order:
      1. The careers_url itself
      2. The careers_url's redirect chain (Workday often redirects company.com/careers
         straight into myworkdayjobs.com)
      3. The HTML body of the careers page (Workday is sometimes embedded via iframe)
    """
    if not careers_url:
        return None

    def _accept(match) -> tuple[str, str, str] | None:
        tenant = match.group(1).lower()
        shard  = match.group(2).lower()
        board  = match.group(3)
        if board.lower() in _WORKDAY_BOARD_BLACKLIST or len(board) < 2:
            return None
        return tenant, board, shard

    # 1. Fast path — the URL already contains the canonical pattern
    m = _WORKDAY_URL_RE.search(careers_url)
    if m and (out := _accept(m)):
        return out

    # 2. Follow redirects and check the final URL
    try:
        r = await client.get(careers_url, headers=_WORKDAY_HEADERS, timeout=_WORKDAY_TIMEOUT)
        m = _WORKDAY_URL_RE.search(str(r.url))
        if m and (out := _accept(m)):
            return out
        # 3. Look for an embedded Workday URL in the HTML — try every match
        # and skip blacklisted boards (e.g. /robots.txt anti-bot links)
        for m in _WORKDAY_URL_RE.finditer(r.text[:60_000]):
            out = _accept(m)
            if out:
                return out
    except Exception:
        return None
    return None


async def _fix_workday_slugs_async(limit: int = 500) -> dict:
    """Scan companies tagged ``ats='workday'`` whose ats_identifier doesn't
    contain a pipe (i.e. only the tenant was captured) and patch them with
    the full ``tenant|board|shard`` triple.  Also catches `ats='unknown'`
    rows whose careers_url points at myworkdayjobs.com."""
    from app.database import AsyncSessionLocal
    fixed = checked = 0

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("""
                SELECT id, name, careers_url, ats_identifier
                FROM companies
                WHERE active = true
                  AND careers_url IS NOT NULL
                  AND careers_url <> ''
                  AND (
                    (ats = 'workday' AND ats_identifier NOT LIKE '%|%')
                    OR (ats = 'unknown' AND careers_url ILIKE '%myworkdayjobs.com%')
                  )
                ORDER BY id
                LIMIT :lim
            """),
            {"lim": limit},
        )).fetchall()

        if not rows:
            logger.info("workday_slug_fix_noop")
            return {"checked": 0, "fixed": 0}

        async with httpx.AsyncClient(follow_redirects=True) as client:
            for row in rows:
                checked += 1
                triple = await _scrape_workday_slug(client, row.careers_url)
                if not triple:
                    continue
                tenant, board, shard = triple
                slug = f"{tenant}|{board}|{shard}"
                try:
                    res = await db.execute(
                        text("""
                            UPDATE companies
                            SET ats = 'workday',
                                ats_identifier = :slug,
                                next_scan_at = NOW(),
                                consecutive_failures = 0,
                                updated_at = NOW()
                            WHERE id = :id
                              AND (ats IN ('workday','unknown'))
                        """),
                        {"slug": slug, "id": row.id},
                    )
                    if res.rowcount:
                        fixed += 1
                        logger.info("workday_slug_fixed", id=row.id, name=row.name[:60], slug=slug)
                except Exception as e:
                    logger.debug("workday_slug_update_failed", id=row.id, err=str(e))
                # Stay polite — Workday + each company's careers page
                await asyncio.sleep(0.3)
            await db.commit()

    logger.info("workday_slug_fix_done", checked=checked, fixed=fixed)
    return {"checked": checked, "fixed": fixed}


@celery_app.task(
    name="app.workers.maintenance_tasks.fix_workday_slugs",
    soft_time_limit=1800, max_retries=1,
)
def fix_workday_slugs(limit: int = 500) -> dict:
    """Backfill `tenant|board|shard` for Workday-tagged companies whose
    ats_identifier only has the tenant.  Runs hourly; each pass covers up
    to `limit` rows."""
    return _run_async(_fix_workday_slugs_async(limit=limit))
