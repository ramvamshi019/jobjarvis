"""
Free Wellfound scraper — no Apify, no recurring cost.

Hits Wellfound's public role listing pages directly via httpx + BeautifulSoup.
For each role URL, paginates through all available jobs and writes them straight
into companies + jobs tables.

Usage inside celery_worker container:
  docker cp backend/scripts/free_wellfound_scraper.py \\
    jobjarvis_celery_worker:/tmp/free_wellfound_scraper.py
  docker exec jobjarvis_celery_worker python3 -u /tmp/free_wellfound_scraper.py

Configure the ROLES list below to control what gets scraped. Default targets
tech entry-friendly roles. Pagination per role goes until we get an empty page
or hit the soft cap (PAGE_LIMIT).

Cost: $0 forever. Just pay your Postgres + bandwidth.
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import httpx
import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# ── Roles to scrape (Wellfound role slugs) ───────────────────────────────────
ROLES = [
    "software-engineer",
    "frontend-engineer",
    "backend-engineer",
    "full-stack-engineer",
    "mobile-engineer",
    "devops-engineer",
    "data-engineer",
    "data-analyst",
    "data-scientist",
    "machine-learning-engineer",
    "product-manager",
    "qa-engineer",
    "security-engineer",
    "designer",
]

PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "20"))   # max pages per role
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))  # concurrent role fetches
DELAY_SEC = float(os.environ.get("DELAY_SEC", "1.5"))  # politeness delay


# ── HTTP fetch with politeness ───────────────────────────────────────────────

async def fetch_role_page(client: httpx.AsyncClient, role: str, page: int) -> dict | None:
    """
    Wellfound's role pages embed initial state JSON in <script> tags.
    They also have a public Algolia-backed search endpoint we can hit.
    Strategy: fetch the public role page HTML and parse __NEXT_DATA__ which
    contains the SSR-hydrated job list as JSON.
    """
    url = f"https://wellfound.com/role/{role}?page={page}"
    try:
        r = await client.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        return parse_next_data(r.text)
    except Exception as e:
        print(f"  [{role} p{page}] fetch error: {e}", flush=True)
        return None


def parse_next_data(html: str) -> dict | None:
    """Extract __NEXT_DATA__ JSON from Wellfound HTML."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        html, re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def extract_jobs(next_data: dict) -> list[dict]:
    """
    Walk the Next.js data tree to find job listings. Wellfound's structure
    changes occasionally; this looks for any list under pageProps that contains
    objects with `slug`, `title`, and `startup`.
    """
    if not next_data:
        return []
    page_props = next_data.get("props", {}).get("pageProps", {})
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            # Job-shaped objects
            if (
                "title" in node
                and ("startup" in node or "company" in node or "company_name" in node)
                and ("id" in node or "jobId" in node or "slug" in node)
            ):
                found.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(page_props)
    return found


def normalize_job(raw: dict, source_role: str) -> dict | None:
    """Coerce a raw Wellfound job dict into the shape we upsert."""
    # Normalize against several possible schemas Wellfound uses.
    company = raw.get("startup") or raw.get("company") or {}
    if isinstance(company, str):
        company = {"name": company, "slug": ""}
    company_name = (
        raw.get("company_name")
        or company.get("name")
        or raw.get("startupName")
    )
    company_slug = (
        raw.get("company_slug")
        or company.get("slug")
        or (raw.get("startupSlug"))
    )
    if not company_name or not company_slug:
        return None

    title = raw.get("title")
    if not title:
        return None

    job_id = str(raw.get("id") or raw.get("jobId") or "")
    if not job_id:
        return None

    slug = raw.get("slug") or job_id
    job_url = f"https://wellfound.com/company/{company_slug}/jobs/{slug}"

    locations = raw.get("locationNames") or raw.get("location_names") or []
    if isinstance(locations, str):
        locations = [locations]
    remote = bool(raw.get("remote") or raw.get("isRemote"))

    salary = raw.get("compensation") or raw.get("salary") or ""
    salary_min = raw.get("salaryMin") or raw.get("compensationMin")
    salary_max = raw.get("salaryMax") or raw.get("compensationMax")

    return {
        "id": job_id,
        "title": title,
        "slug": slug,
        "url": job_url,
        "applyUrl": job_url,
        "company_name": company_name,
        "company_slug": company_slug,
        "location_names": locations,
        "remote": remote,
        "compensation": salary,
        "base_salary": {
            "min_value": salary_min,
            "max_value": salary_max,
            "currency": "USD",
            "unit": "YEAR",
        },
        "description": raw.get("description") or raw.get("descriptionFormatted") or "",
        "job_type": (raw.get("jobType") or raw.get("type") or "").lower() or None,
        "primary_role_title": (raw.get("primaryRoleTitle") or source_role).lower(),
        "ats_source": raw.get("atsSource"),
        "live_start_at": raw.get("liveStartAt") or raw.get("postedAt"),
        "years_experience_min": raw.get("yearsExperienceMin"),
    }


# ── DB upserts (mirror import_apify_wellfound.py) ────────────────────────────

def parse_location(names):
    if not names:
        return None, None, None, None
    loc = names[0] if isinstance(names, list) else names
    parts = [p.strip() for p in loc.split(",")]
    return loc, (parts[0] if parts else None), (parts[1] if len(parts) > 1 else None), \
        (parts[-1] if len(parts) > 2 else None)


def map_ats(ats_source):
    if not ats_source:
        return "wellfound"
    s = ats_source.lower()
    for k in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters",
             "icims", "bamboohr", "teamtailor", "recruitee", "workday"):
        if k in s:
            return k
    return "wellfound"


async def upsert_company(conn, job: dict) -> int | None:
    name, slug = job["company_name"], job["company_slug"]
    ats = map_ats(job.get("ats_source"))
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
                      0, 0, 0, false, true, $8, $8)
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
        return None


async def upsert_job(conn, company_id: int, job: dict) -> bool:
    title = job["title"]
    job_url = job["url"]
    ext_id = job["id"]
    location_full, city, region, country = parse_location(job.get("location_names"))
    base_salary = job.get("base_salary") or {}
    salary_min = base_salary.get("min_value") if isinstance(base_salary.get("min_value"), (int, float)) else None
    salary_max = base_salary.get("max_value") if isinstance(base_salary.get("max_value"), (int, float)) else None
    salary_min = int(salary_min) if salary_min and 0 < salary_min < 50_000_000 else None
    salary_max = int(salary_max) if salary_max and 0 < salary_max < 50_000_000 else None

    posted_at = None
    if job.get("live_start_at"):
        try:
            posted_at = datetime.fromtimestamp(int(job["live_start_at"]), timezone.utc)
        except Exception:
            posted_at = None

    now = datetime.now(timezone.utc)
    remote_type = "remote" if job.get("remote") else "onsite"
    description = (job.get("description") or "")[:50000] or None
    employment_type = job.get("job_type")
    role_category = job.get("primary_role_title")

    try:
        await conn.execute(
            """
            INSERT INTO jobs (
                company_id, external_id, title, company_name,
                location, city, region, country, remote_type,
                url, apply_url, description,
                employment_type, role_category,
                salary_min, salary_max, salary_currency, salary_period,
                source, source_type, source_confidence,
                active, data_quality_score,
                spam_score, eligibility_risk_score,
                first_seen_at, last_seen_at, posted_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11, $12,
                $13, $14,
                $15, $16, 'USD', 'annual',
                'wellfound', 'wellfound', 1.0,
                true, 0.95, 0.0, 0.0,
                $17, $17, $18
            )
            ON CONFLICT (url) DO UPDATE SET
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                salary_min = EXCLUDED.salary_min,
                salary_max = EXCLUDED.salary_max,
                last_seen_at = EXCLUDED.last_seen_at,
                posted_at = COALESCE(EXCLUDED.posted_at, jobs.posted_at),
                active = true,
                updated_at = EXCLUDED.last_seen_at
            """,
            company_id, ext_id, title[:500], job["company_name"][:500],
            (location_full or "")[:500], (city or "")[:100] or None,
            (region or "")[:100] or None, (country or "")[:100] or None,
            remote_type, job_url[:5000], job["applyUrl"][:5000], description,
            (employment_type or "")[:100] or None, (role_category or "")[:100] or None,
            salary_min, salary_max,
            now, posted_at,
        )
        return True
    except Exception as e:
        return False


# ── Scrape orchestration ─────────────────────────────────────────────────────

async def scrape_role(client: httpx.AsyncClient, conn, role: str, sem: asyncio.Semaphore) -> dict:
    stats = {"role": role, "pages": 0, "jobs_seen": 0, "companies_inserted": 0, "jobs_inserted": 0}
    seen_companies: dict[str, int] = {}
    async with sem:
        for page in range(1, PAGE_LIMIT + 1):
            data = await fetch_role_page(client, role, page)
            if not data:
                break
            jobs = extract_jobs(data)
            if not jobs:
                break
            stats["pages"] += 1
            stats["jobs_seen"] += len(jobs)
            for raw in jobs:
                norm = normalize_job(raw, role)
                if not norm:
                    continue
                slug = norm["company_slug"]
                cid = seen_companies.get(slug)
                if cid is None:
                    cid = await upsert_company(conn, norm)
                    if cid is None:
                        continue
                    seen_companies[slug] = cid
                    stats["companies_inserted"] += 1
                if await upsert_job(conn, cid, norm):
                    stats["jobs_inserted"] += 1
            await asyncio.sleep(DELAY_SEC)
    return stats


async def main():
    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)
    print(f"Scraping {len(ROLES)} roles, max {PAGE_LIMIT} pages each, "
          f"concurrency={CONCURRENCY}\n", flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:
        tasks = [scrape_role(client, conn, r, sem) for r in ROLES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    await conn.close()
    print("\n" + "=" * 60, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 60, flush=True)
    total_co, total_j = 0, 0
    for r in results:
        if isinstance(r, dict):
            print(f"  {r['role']:30s}  pages={r['pages']:>2}  "
                  f"jobs={r['jobs_inserted']:>4}  companies={r['companies_inserted']:>4}",
                  flush=True)
            total_co += r["companies_inserted"]
            total_j += r["jobs_inserted"]
    print(f"\n  TOTAL  companies={total_co}  jobs={total_j}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
