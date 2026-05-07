"""
Import Y Combinator companies + jobs from an Apify dataset into JobJarvis.

Run inside the celery_worker container (has httpx + asyncpg):
  docker cp backend/scripts/import_apify_yc.py \\
      jobjarvis_celery_worker:/tmp/import_apify_yc.py
  docker exec -e APIFY_TOKEN=<token> -e APIFY_DATASET_ID=<dataset_id> \\
      jobjarvis_celery_worker python3 -u /tmp/import_apify_yc.py

Or, for testing without Apify, set APIFY_LOCAL_FILE to a local JSON path.

Each YC company gets upserted to companies (ats='yc'); each item in the
company's openJobs[] gets upserted to jobs.
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import httpx
import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
APIFY_DATASET_ID = os.environ.get("APIFY_DATASET_ID", "")
APIFY_LOCAL_FILE = os.environ.get("APIFY_LOCAL_FILE", "")  # for offline testing


# ─── Apify dataset reader ─────────────────────────────────────────────────────

async def fetch_apify_dataset(dataset_id: str, token: str) -> list[dict]:
    """Stream all items from an Apify dataset (paginated)."""
    items: list[dict] = []
    offset = 0
    page_size = 1000
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=60) as c:
        while True:
            url = (
                f"https://api.apify.com/v2/datasets/{dataset_id}/items"
                f"?clean=1&offset={offset}&limit={page_size}"
            )
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            items.extend(page)
            print(f"  fetched {len(items)} items so far…", flush=True)
            if len(page) < page_size:
                break
            offset += page_size
    return items


def load_local(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("items", [])


# ─── Normalization helpers ────────────────────────────────────────────────────

def parse_location(loc: str | None):
    """Heuristic: split 'San Francisco, CA, US' into city/region/country."""
    if not loc:
        return None, None, None
    parts = [p.strip() for p in loc.split(",")]
    city = parts[0] if len(parts) >= 1 else None
    region = parts[1] if len(parts) >= 2 else None
    country = parts[-1] if len(parts) >= 3 else (parts[1] if len(parts) == 2 else None)
    return city, region, country


def derive_remote_type(job: dict) -> str:
    if job.get("remote") is True:
        return "remote"
    loc = (job.get("location") or "").lower()
    if "remote" in loc:
        return "remote" if "/" not in loc else "hybrid"
    return "onsite"


def job_external_id(job_url: str) -> str:
    """Extract a stable ID from the YC job URL slug."""
    m = re.search(r"/jobs/([A-Za-z0-9_\-]+)", job_url or "")
    return m.group(1) if m else (job_url[-100:] if job_url else "")


def safe_salary(v) -> int | None:
    """YC sometimes returns INR amounts >999k for non-US roles. Cap absurd values."""
    if not isinstance(v, (int, float)):
        return None
    v = int(v)
    if v <= 0 or v > 50_000_000:
        return None
    return v


# ─── DB upserts ───────────────────────────────────────────────────────────────

async def upsert_company(conn, item: dict) -> int | None:
    """Returns company_id on success."""
    name = item.get("name")
    slug = item.get("slug") or item.get("id")
    website = item.get("website") or item.get("url")
    if not name or not website:
        return None

    now = datetime.now(timezone.utc)
    next_scan = now + timedelta(minutes=30)

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO companies (
                name, ats, ats_identifier, careers_url,
                priority_score, scan_priority,
                scan_frequency_minutes, next_scan_at, active,
                failure_count, consecutive_failures, jobs_found_count,
                is_blocklisted, robots_txt_compliant,
                created_at, updated_at
            ) VALUES ($1, 'yc', $2, $3,
                      $4::integer, $4::double precision, $5, $6, true,
                      0, 0, 0,
                      false, true,
                      $7, $7)
            ON CONFLICT (name) DO UPDATE SET
                ats = CASE
                    WHEN companies.ats IS NULL OR companies.ats = ''
                        THEN EXCLUDED.ats
                    ELSE companies.ats
                END,
                ats_identifier = COALESCE(NULLIF(companies.ats_identifier, ''), EXCLUDED.ats_identifier),
                careers_url    = COALESCE(NULLIF(companies.careers_url, ''), EXCLUDED.careers_url),
                active = true,
                updated_at = EXCLUDED.updated_at
            RETURNING id
            """,
            name, slug, website, 60, 360, next_scan, now,
        )
        return row["id"] if row else None
    except Exception as e:
        print(f"  company upsert error for {name}: {type(e).__name__}: {e}", flush=True)
        return None


async def upsert_job(conn, company_id: int, company_name: str, job: dict) -> bool:
    """Upsert a single job posting into the jobs table."""
    title = job.get("title")
    job_url = job.get("url")
    if not title or not job_url:
        return False

    ext_id = job_external_id(job_url)
    location = job.get("location")
    city, region, country = parse_location(location)
    remote_type = derive_remote_type(job)
    apply_url = job.get("applyUrl") or job_url
    employment_type = (job.get("type") or "").lower() or None
    role_category = (job.get("role") or "").lower() or None
    skills = job.get("skills") or []
    salary_min = safe_salary(job.get("salaryMin"))
    salary_max = safe_salary(job.get("salaryMax"))
    salary_currency = "USD" if (country or "").lower() in ("us", "usa", "united states") else None
    now = datetime.now(timezone.utc)

    try:
        await conn.execute(
            """
            INSERT INTO jobs (
                company_id, external_id, title, company_name,
                location, city, region, country, remote_type,
                url, apply_url,
                employment_type, role_category,
                salary_min, salary_max, salary_currency,
                required_skills,
                source, source_type, source_confidence,
                active, data_quality_score,
                spam_score, eligibility_risk_score,
                first_seen_at, last_seen_at, posted_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11,
                $12, $13,
                $14, $15, $16,
                $17::jsonb,
                'yc', 'yc', 1.0,
                true, 0.9,
                0.0, 0.0,
                $18, $18, NULL
            )
            ON CONFLICT (company_id, external_id) DO UPDATE SET
                title          = EXCLUDED.title,
                location       = EXCLUDED.location,
                city           = EXCLUDED.city,
                region         = EXCLUDED.region,
                country        = EXCLUDED.country,
                remote_type    = EXCLUDED.remote_type,
                apply_url      = EXCLUDED.apply_url,
                employment_type= EXCLUDED.employment_type,
                role_category  = EXCLUDED.role_category,
                salary_min     = EXCLUDED.salary_min,
                salary_max     = EXCLUDED.salary_max,
                salary_currency= EXCLUDED.salary_currency,
                required_skills= EXCLUDED.required_skills,
                last_seen_at   = EXCLUDED.last_seen_at,
                active         = true,
                updated_at     = EXCLUDED.last_seen_at
            """,
            company_id, ext_id, title[:500], company_name[:500],
            (location or "")[:500], (city or "")[:100] or None,
            (region or "")[:100] or None, (country or "")[:100] or None,
            remote_type,
            job_url[:5000], apply_url[:5000],
            employment_type[:100] if employment_type else None,
            role_category[:100] if role_category else None,
            salary_min, salary_max, salary_currency,
            json.dumps(skills) if skills else "[]",
            now,
        )
        return True
    except Exception as e:
        print(f"  job upsert error for {title}: {type(e).__name__}: {e}", flush=True)
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

    # Load items
    if APIFY_LOCAL_FILE:
        print(f"Loading from local file: {APIFY_LOCAL_FILE}", flush=True)
        items = load_local(APIFY_LOCAL_FILE)
    elif APIFY_DATASET_ID:
        print(f"Fetching Apify dataset: {APIFY_DATASET_ID}", flush=True)
        items = await fetch_apify_dataset(APIFY_DATASET_ID, APIFY_TOKEN)
    else:
        print("ERROR: set APIFY_DATASET_ID (with APIFY_TOKEN) or APIFY_LOCAL_FILE", flush=True)
        await conn.close()
        sys.exit(1)

    print(f"Loaded {len(items)} dataset items", flush=True)

    # Filter to actual company items (not job-only items, in case)
    companies = [i for i in items if i.get("type") == "yc_company" or i.get("name")]
    print(f"  {len(companies)} are companies", flush=True)

    co_inserted = 0
    co_errors = 0
    job_inserted = 0
    job_errors = 0

    for item in companies:
        cid = await upsert_company(conn, item)
        if cid is None:
            co_errors += 1
            continue
        co_inserted += 1

        # Insert open jobs
        for job in item.get("openJobs", []) or []:
            if await upsert_job(conn, cid, item.get("name", ""), job):
                job_inserted += 1
            else:
                job_errors += 1

    await conn.close()

    print()
    print("=" * 60, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 60, flush=True)
    print(f"  Companies upserted:  {co_inserted:>5}  (errors: {co_errors})", flush=True)
    print(f"  Jobs upserted:       {job_inserted:>5}  (errors: {job_errors})", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
