"""
Standalone job scanner — bypass Celery, hit every company's ATS API directly,
upsert jobs into postgres.

Why this exists:
  Celery's _run_async wrapper has a fork+asyncpg deadlock that silently stalls
  the scheduled scan task.  This script does the same job synchronously with
  bounded concurrency, runs once, and exits clean.

Covers 7 ATS types with public JSON APIs (no auth):
  greenhouse, lever, ashby, workable, smartrecruiters, recruitee, teamtailor
That's ~63% of the 12k companies in your DB, ~7,800 companies.

Usage:
  docker cp backend/scripts/scan_all_jobs.py jobjarvis_celery_worker:/tmp/
  docker exec jobjarvis_celery_worker python3 -u /tmp/scan_all_jobs.py

Optional flags:
  --limit N        scan at most N companies (default: all)
  --concurrency K  parallel requests (default: 20)
  --ats greenhouse only scan one ATS (debug)
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional
import json

import asyncpg
import httpx


DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


# ── ATS-specific fetchers ────────────────────────────────────────────────────
# Each returns a list[dict] of normalized job rows ready to insert.

async def _fetch(client: httpx.AsyncClient, url: str, timeout: float = 15) -> Any:
    try:
        r = await client.get(url, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _normalize_job(
    *, external_id, title, company_name, location="", url="",
    apply_url="", description="", remote=False, posted_at=None,
) -> dict | None:
    if not external_id or not title:
        return None
    return {
        "external_id":     str(external_id)[:500],
        "title":           str(title)[:500],
        "company_name":    str(company_name)[:500],
        "location":        (location or "")[:500],
        "url":             (url or "")[:5000],
        "apply_url":       (apply_url or url or "")[:5000],
        "description":     (description or "")[:50000],
        "remote_type":     "remote" if remote else "onsite",
        "posted_at":       posted_at,
    }


async def fetch_greenhouse(client, slug, company_name):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = await _fetch(client, url)
    if not data or not isinstance(data, dict):
        return []
    jobs = []
    for j in data.get("jobs", []):
        offices = j.get("offices") or []
        location = ", ".join(o.get("name", "") for o in offices if o.get("name"))
        if not location:
            location = (j.get("location") or {}).get("name", "")
        remote = any("remote" in (o.get("name") or "").lower() for o in offices)
        posted = None
        if j.get("updated_at"):
            try:
                posted = datetime.fromisoformat(j["updated_at"].replace("Z", "+00:00"))
            except Exception:
                pass
        n = _normalize_job(
            external_id=j.get("id"),
            title=j.get("title"),
            company_name=company_name,
            location=location,
            url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url", ""),
            description=j.get("content", "") or "",
            remote=remote,
            posted_at=posted,
        )
        if n:
            jobs.append(n)
    return jobs


async def fetch_lever(client, slug, company_name):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=250"
    data = await _fetch(client, url)
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        location = cats.get("location", "")
        remote = (cats.get("commitment", "") or "").lower() == "remote" \
                 or "remote" in (location or "").lower()
        posted = None
        if j.get("createdAt"):
            try:
                posted = datetime.fromtimestamp(j["createdAt"]/1000, tz=timezone.utc)
            except Exception:
                pass
        desc = j.get("descriptionPlain") or j.get("description") or ""
        n = _normalize_job(
            external_id=j.get("id"),
            title=j.get("text"),
            company_name=company_name,
            location=location,
            url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
            description=desc,
            remote=remote,
            posted_at=posted,
        )
        if n:
            jobs.append(n)
    return jobs


async def fetch_ashby(client, slug, company_name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    data = await _fetch(client, url)
    if not isinstance(data, dict):
        return []
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("locationName") or ""
        remote = j.get("isRemote", False) or "remote" in loc.lower()
        posted = None
        if j.get("publishedAt"):
            try:
                posted = datetime.fromisoformat(j["publishedAt"].replace("Z", "+00:00"))
            except Exception:
                pass
        n = _normalize_job(
            external_id=j.get("id"),
            title=j.get("title"),
            company_name=company_name,
            location=loc,
            url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            description=j.get("descriptionHtml") or j.get("descriptionPlain", ""),
            remote=remote,
            posted_at=posted,
        )
        if n:
            jobs.append(n)
    return jobs


async def fetch_workable(client, slug, company_name):
    url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    body = {"query": "", "department": [], "location": [], "remote": []}
    try:
        r = await client.post(url, json=body, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    jobs = []
    for j in data.get("results", []):
        loc_obj = j.get("location") or {}
        location = ", ".join(filter(None, [
            loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country"),
        ]))
        remote = bool(j.get("remote") or j.get("workplace") == "remote")
        posted = None
        if j.get("published"):
            try:
                posted = datetime.fromisoformat(j["published"].replace("Z", "+00:00"))
            except Exception:
                pass
        job_url = f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}"
        n = _normalize_job(
            external_id=j.get("shortcode"),
            title=j.get("title"),
            company_name=company_name,
            location=location,
            url=job_url,
            apply_url=job_url,
            description=j.get("description", ""),
            remote=remote,
            posted_at=posted,
        )
        if n:
            jobs.append(n)
    return jobs


async def fetch_smartrecruiters(client, slug, company_name):
    jobs = []
    offset = 0
    while True:
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?status=PUBLIC&limit=100&offset={offset}"
        data = await _fetch(client, url)
        if not isinstance(data, dict):
            break
        items = data.get("content", [])
        if not items:
            break
        for j in items:
            loc_obj = j.get("location") or {}
            location = ", ".join(filter(None, [
                loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country"),
            ]))
            posted = None
            if j.get("releasedDate"):
                try:
                    posted = datetime.fromisoformat(j["releasedDate"].replace("Z", "+00:00"))
                except Exception:
                    pass
            job_url = (j.get("ref") or "")
            n = _normalize_job(
                external_id=j.get("id"),
                title=j.get("name"),
                company_name=company_name,
                location=location,
                url=job_url,
                apply_url=job_url,
                description="",
                remote=False,
                posted_at=posted,
            )
            if n:
                jobs.append(n)
        if len(items) < 100:
            break
        offset += 100
        if offset > 500:
            break
    return jobs


async def fetch_recruitee(client, slug, company_name):
    url = f"https://{slug}.recruitee.com/api/offers/?scope=published&limit=100"
    data = await _fetch(client, url)
    if not isinstance(data, dict):
        return []
    jobs = []
    for j in data.get("offers", []):
        location = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        remote = bool(j.get("remote"))
        posted = None
        if j.get("published_at"):
            try:
                posted = datetime.fromisoformat(j["published_at"].replace("Z", "+00:00"))
            except Exception:
                pass
        n = _normalize_job(
            external_id=j.get("id"),
            title=j.get("title"),
            company_name=company_name,
            location=location,
            url=j.get("careers_url") or j.get("careers_apply_url") or "",
            apply_url=j.get("careers_apply_url") or j.get("careers_url") or "",
            description=j.get("description", ""),
            remote=remote,
            posted_at=posted,
        )
        if n:
            jobs.append(n)
    return jobs


async def fetch_teamtailor(client, slug, company_name):
    url = f"https://{slug}.teamtailor.com/jobs.json"
    data = await _fetch(client, url)
    if not data:
        return []
    if isinstance(data, dict):
        items = data.get("jobs") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    jobs = []
    base = f"https://{slug}.teamtailor.com"
    for j in items:
        attrs = j.get("attributes", j) if isinstance(j, dict) else {}
        title = attrs.get("title") or attrs.get("name") or ""
        if not title:
            continue
        n = _normalize_job(
            external_id=j.get("id") if isinstance(j, dict) else None,
            title=title,
            company_name=company_name,
            location=attrs.get("city") or attrs.get("location") or "",
            url=attrs.get("url") or f"{base}/jobs/{j.get('id', '')}",
            apply_url=attrs.get("apply_url") or attrs.get("url") or "",
            description=attrs.get("body", "") or attrs.get("description", ""),
            remote=bool(attrs.get("remote")),
        )
        if n:
            jobs.append(n)
    return jobs


FETCHERS = {
    "greenhouse":      fetch_greenhouse,
    "lever":           fetch_lever,
    "ashby":           fetch_ashby,
    "workable":        fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee":       fetch_recruitee,
    "teamtailor":      fetch_teamtailor,
}


# ── DB upsert ────────────────────────────────────────────────────────────────

async def upsert_jobs(pool, company_id: int, jobs: list[dict]) -> int:
    sql = """
        INSERT INTO jobs (
            company_id, external_id, title, company_name, location,
            remote_type, url, apply_url, description, posted_at,
            spam_score, eligibility_risk_score, source_confidence,
            first_seen_at, last_seen_at, active, created_at, updated_at
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9, $10,
            0.0, 0.0, 0.8,
            NOW(), NOW(), true, NOW(), NOW()
        )
        ON CONFLICT (url) DO UPDATE SET
            last_seen_at = NOW(),
            active = true,
            updated_at = NOW()
    """
    inserted = 0
    async with pool.acquire() as conn:
        for j in jobs or []:
            if not j.get("url"):
                continue
            try:
                await conn.execute(
                    sql,
                    company_id, j["external_id"], j["title"], j["company_name"],
                    j["location"], j["remote_type"], j["url"], j["apply_url"],
                    j["description"], j["posted_at"],
                )
                inserted += 1
            except asyncpg.exceptions.UniqueViolationError:
                pass
            except Exception:
                pass
        # Mark company as scanned
        await conn.execute(
            """
            UPDATE companies SET
                last_seen_at = NOW(),
                jobs_found_count = jobs_found_count + $2,
                consecutive_failures = 0,
                next_scan_at = NOW() + INTERVAL '6 hours',
                updated_at = NOW()
            WHERE id = $1
            """,
            company_id, len(jobs or []),
        )
    return inserted


async def mark_failure(pool, company_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE companies SET
                consecutive_failures = consecutive_failures + 1,
                failure_count = failure_count + 1,
                next_scan_at = NOW() + INTERVAL '12 hours',
                updated_at = NOW()
            WHERE id = $1
            """,
            company_id,
        )


# ── Main ─────────────────────────────────────────────────────────────────────

async def scan_one(sem, client, pool, comp) -> tuple[int, int]:
    async with sem:
        cid = comp["id"]
        ats = comp["ats"]
        slug = comp["ats_identifier"]
        cname = comp["name"]
        if not slug or ats not in FETCHERS:
            return (cid, 0)
        try:
            jobs = await FETCHERS[ats](client, slug, cname)
            n = await upsert_jobs(pool, cid, jobs)
            return (cid, n)
        except Exception:
            try:
                await mark_failure(pool, cid)
            except Exception:
                pass
            return (cid, 0)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",       type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--ats",         type=str, default=None,
                        help="Only scan one ATS type (debug)")
    args = parser.parse_args()

    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    pool = await asyncpg.create_pool(
        DB_DSN, min_size=2, max_size=args.concurrency + 5,
        command_timeout=30,
    )

    where = "active = true AND ats = ANY($1::text[])"
    params: list = [list(FETCHERS.keys())]
    if args.ats:
        params = [[args.ats]]

    if args.limit:
        sql = f"SELECT id, name, ats, ats_identifier FROM companies WHERE {where} LIMIT {args.limit}"
    else:
        sql = f"SELECT id, name, ats, ats_identifier FROM companies WHERE {where}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    print(f"Will scan {len(rows)} companies "
          f"(concurrency={args.concurrency}, ATS types={list(FETCHERS.keys())})\n",
          flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.time()
    total_jobs = 0
    scanned = 0
    failed_zero = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0"},
        http2=False,
        timeout=20,
    ) as client:
        coros = [scan_one(sem, client, pool, dict(r)) for r in rows]
        for fut in asyncio.as_completed(coros):
            cid, n = await fut
            scanned += 1
            total_jobs += n
            if n == 0:
                failed_zero += 1
            if scanned % 50 == 0:
                elapsed = time.time() - t0
                rate = scanned / elapsed if elapsed else 0
                eta = (len(rows) - scanned) / rate if rate else 0
                print(f"  {scanned:>5}/{len(rows)}  "
                      f"+jobs={total_jobs}  empty={failed_zero}  "
                      f"({rate:.1f} co/s, ETA {eta/60:.1f}m)", flush=True)

    elapsed = time.time() - t0
    print(f"\n=== DONE ===", flush=True)
    print(f"  scanned:       {scanned}", flush=True)
    print(f"  jobs upserted: {total_jobs}", flush=True)
    print(f"  zero-result:   {failed_zero}  (defunct / not hiring)", flush=True)
    print(f"  elapsed:       {elapsed/60:.1f}m  ({scanned/elapsed:.1f} co/s)",
          flush=True)

    async with pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT (SELECT COUNT(*) FROM jobs WHERE active=true) AS jobs, "
            "       (SELECT COUNT(*) FROM companies WHERE active=true) AS cos"
        )
    print(f"\nDB now: {after['jobs']} active jobs across {after['cos']} active companies",
          flush=True)
    await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
