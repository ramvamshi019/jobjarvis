"""
Import Wellfound jobs from an Apify dataset into JobJarvis.

Run inside the celery_worker container:
  docker cp backend/scripts/import_apify_wellfound.py \\
      jobjarvis_celery_worker:/tmp/import_apify_wellfound.py
  docker exec -e APIFY_TOKEN=<token> -e APIFY_DATASET_ID=<dataset_id> \\
      jobjarvis_celery_worker python3 -u /tmp/import_apify_wellfound.py

Each Wellfound job record contains both job and company data. We:
  • Upsert the company into companies (ats='wellfound' if no ats_source,
    otherwise ats=<ats_source> like 'greenhouse' / 'ashby' / 'lever')
  • Upsert the job into jobs with source='wellfound'
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
APIFY_LOCAL_FILE = os.environ.get("APIFY_LOCAL_FILE", "")


# ─── Apify dataset reader (paginated) ────────────────────────────────────────

async def fetch_apify_dataset(dataset_id: str, token: str) -> list[dict]:
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

def parse_location(names: list[str] | None):
    """Wellfound returns location_names as an array. Pick the first."""
    if not names:
        return None, None, None, None
    loc = names[0]
    parts = [p.strip() for p in loc.split(",")]
    city = parts[0] if parts else None
    region = parts[1] if len(parts) > 1 else None
    country = parts[-1] if len(parts) > 2 else None
    full = ", ".join(parts)
    return full, city, region, country


def derive_remote_type(job: dict) -> str:
    if job.get("remote") is True:
        loc = (job.get("location_names") or [""])[0].lower()
        if loc and loc not in ("united states", "remote", "global", ""):
            return "hybrid"
        return "remote"
    return "onsite"


def map_ats_from_wellfound(ats_source: str | None) -> str:
    """Wellfound's ats_source field tells us the underlying ATS."""
    if not ats_source:
        return "wellfound"  # Wellfound-native, no external ATS
    s = ats_source.lower()
    if "greenhouse" in s:
        return "greenhouse"
    if "ashby" in s:
        return "ashby"
    if "lever" in s:
        return "lever"
    if "workable" in s:
        return "workable"
    if "smartrecruiters" in s:
        return "smartrecruiters"
    if "icims" in s:
        return "icims"
    if "bamboohr" in s:
        return "bamboohr"
    if "teamtailor" in s:
        return "teamtailor"
    if "recruitee" in s:
        return "recruitee"
    if "workday" in s:
        return "workday"
    return "wellfound"


def safe_salary(v) -> int | None:
    if not isinstance(v, (int, float)):
        return None
    v = int(v)
    if v <= 0 or v > 50_000_000:
        return None
    return v


# ─── DB upserts ───────────────────────────────────────────────────────────────

async def upsert_company(conn, job: dict) -> int | None:
    name = job.get("company_name")
    slug = job.get("company_slug")
    if not name or not slug:
        return None

    ats = map_ats_from_wellfound(job.get("ats_source"))
    careers_url = f"https://wellfound.com/company/{slug}/jobs"
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
            ) VALUES ($1, $2, $3, $4,
                      $5::integer, $5::double precision, $6, $7, true,
                      0, 0, 0,
                      false, true,
                      $8, $8)
            ON CONFLICT (name) DO UPDATE SET
                careers_url = COALESCE(NULLIF(companies.careers_url, ''), EXCLUDED.careers_url),
                active = true,
                updated_at = EXCLUDED.updated_at
            RETURNING id
            """,
            name[:500], ats, slug[:500], careers_url[:5000],
            55, 360, next_scan, now,
        )
        return row["id"] if row else None
    except Exception as e:
        print(f"  company upsert error for {name}: {type(e).__name__}: {e}", flush=True)
        return None


async def upsert_job(conn, company_id: int, job: dict) -> bool:
    title = job.get("title")
    job_url = job.get("url")
    if not title or not job_url:
        return False

    ext_id = str(job.get("id", ""))
    if not ext_id:
        return False

    company_name = job.get("company_name", "")
    location_full, city, region, country = parse_location(job.get("location_names"))
    remote_type = derive_remote_type(job)
    apply_url = job_url
    employment_type = (job.get("job_type") or "").lower() or None
    role_category = (job.get("primary_role_title") or "").lower() or None

    base_salary = job.get("base_salary") or {}
    salary_min = safe_salary(base_salary.get("min_value"))
    salary_max = safe_salary(base_salary.get("max_value"))
    salary_currency = base_salary.get("currency") or None
    salary_period = (base_salary.get("unit") or "").lower() or None
    if salary_period == "year":
        salary_period = "annual"

    description = job.get("description", "")
    if description and len(description) > 50000:
        description = description[:50000]

    posted_at = None
    if job.get("live_start_at"):
        try:
            posted_at = datetime.fromtimestamp(job["live_start_at"], timezone.utc)
        except Exception:
            posted_at = None

    now = datetime.now(timezone.utc)

    try:
        await conn.execute(
            """
            INSERT INTO jobs (
                company_id, external_id, title, company_name,
                location, city, region, country, remote_type,
                url, apply_url,
                description,
                employment_type, role_category,
                salary_min, salary_max, salary_currency, salary_period,
                source, source_type, source_confidence,
                active, data_quality_score,
                spam_score, eligibility_risk_score,
                first_seen_at, last_seen_at, posted_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11,
                $12,
                $13, $14,
                $15, $16, $17, $18,
                'wellfound', 'wellfound', 1.0,
                true, 0.95,
                0.0, 0.0,
                $19, $19, $20
            )
            ON CONFLICT (url) DO UPDATE SET
                title          = EXCLUDED.title,
                location       = EXCLUDED.location,
                city           = EXCLUDED.city,
                region         = EXCLUDED.region,
                country        = EXCLUDED.country,
                remote_type    = EXCLUDED.remote_type,
                description    = EXCLUDED.description,
                employment_type= EXCLUDED.employment_type,
                role_category  = EXCLUDED.role_category,
                salary_min     = EXCLUDED.salary_min,
                salary_max     = EXCLUDED.salary_max,
                salary_currency= EXCLUDED.salary_currency,
                salary_period  = EXCLUDED.salary_period,
                last_seen_at   = EXCLUDED.last_seen_at,
                posted_at      = COALESCE(EXCLUDED.posted_at, jobs.posted_at),
                active         = true,
                updated_at     = EXCLUDED.last_seen_at
            """,
            company_id, ext_id, title[:500], company_name[:500],
            (location_full or "")[:500],
            (city or "")[:100] or None,
            (region or "")[:100] or None,
            (country or "")[:100] or None,
            remote_type,
            job_url[:5000], apply_url[:5000],
            description or None,
            employment_type[:100] if employment_type else None,
            role_category[:100] if role_category else None,
            salary_min, salary_max,
            (salary_currency or "")[:10] or None,
            (salary_period or "")[:20] or None,
            now, posted_at,
        )
        return True
    except Exception as e:
        print(f"  job upsert error for {title}: {type(e).__name__}: {e}", flush=True)
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

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

    co_inserted = 0
    co_errors = 0
    job_inserted = 0
    job_errors = 0
    seen_companies: dict[str, int] = {}  # cache: company_slug -> company_id

    # Group by ats source for reporting
    ats_breakdown: dict[str, int] = {}

    for item in items:
        slug = item.get("company_slug")
        if not slug:
            continue

        if slug in seen_companies:
            cid = seen_companies[slug]
        else:
            cid = await upsert_company(conn, item)
            if cid is None:
                co_errors += 1
                continue
            seen_companies[slug] = cid
            co_inserted += 1
            ats = map_ats_from_wellfound(item.get("ats_source"))
            ats_breakdown[ats] = ats_breakdown.get(ats, 0) + 1

        if await upsert_job(conn, cid, item):
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
    print()
    print("  Companies by ATS source:", flush=True)
    for ats, count in sorted(ats_breakdown.items(), key=lambda x: -x[1]):
        print(f"    {ats:18s} {count:>5}", flush=True)


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
