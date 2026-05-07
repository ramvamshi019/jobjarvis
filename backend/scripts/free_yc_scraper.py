"""
Free Y Combinator scraper — no Apify, no recurring cost.

YC's company directory is backed by a public Algolia search index. We hit the
same index directly that the Apify actor uses. Iterates per-batch to bypass
Algolia's 1000-result-per-query cap.

Usage:
  docker cp backend/scripts/free_yc_scraper.py \\
    jobjarvis_celery_worker:/tmp/free_yc_scraper.py
  docker exec jobjarvis_celery_worker python3 -u /tmp/free_yc_scraper.py

Run weekly via Celery beat to catch new batches. $0 ongoing cost.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

import httpx
import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")

# Public Algolia config used by ycombinator.com/companies
ALGOLIA_APP_ID = "45BWZJ1SGC"
ALGOLIA_API_KEY = "Mjc4MmJkN2JkYzdiY2JlYmRhMDQ1MTcwOTc1ZjQ4ZDFiOGE3MTRmMDM2NDc5OTNiZGRkNGQ1MjE4YjRmZTcyZnRhZ0ZpbHRlcnM9"
ALGOLIA_INDEX = "YCCompany_production"
ALGOLIA_URL = (
    f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/"
    f"{ALGOLIA_INDEX}/query"
)

# All YC batch codes (W05 = Winter 2005, S05 = Summer 2005, etc.)
# Updated as new batches are announced
BATCHES = [
    "S05","W06","S06","W07","S07","W08","S08","W09","S09",
    "W10","S10","W11","S11","W12","S12","W13","S13",
    "W14","S14","W15","S15","W16","S16","W17","S17",
    "W18","S18","W19","S19","W20","S20","W21","S21",
    "W22","S22","W23","S23","W24","S24","F24","W25","S25","F25",
]


async def algolia_query(client: httpx.AsyncClient, batch: str) -> list[dict]:
    """Hit Algolia's REST API directly. No SDK required."""
    body = {
        "query": "",
        "hitsPerPage": 1000,
        "filters": f'batch:"{batch}" AND isHiring:true',
    }
    headers = {
        "X-Algolia-Application-Id": ALGOLIA_APP_ID,
        "X-Algolia-API-Key": ALGOLIA_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        r = await client.post(ALGOLIA_URL, json=body, headers=headers, timeout=30)
        if r.status_code != 200:
            return []
        return r.json().get("hits", [])
    except Exception as e:
        print(f"  [{batch}] error: {e}", flush=True)
        return []


async def upsert_company(conn, hit: dict) -> int | None:
    name = hit.get("name")
    slug = hit.get("slug") or hit.get("id")
    website = hit.get("website")
    if not name or not slug:
        return None
    careers_url = website or f"https://www.ycombinator.com/companies/{slug}"
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
                      0, 0, 0, false, true, $7, $7)
            ON CONFLICT (name) DO UPDATE SET
                ats_identifier = COALESCE(NULLIF(companies.ats_identifier,''), EXCLUDED.ats_identifier),
                careers_url    = COALESCE(NULLIF(companies.careers_url,''), EXCLUDED.careers_url),
                active = true,
                updated_at = EXCLUDED.updated_at
            RETURNING id
            """,
            name[:500], slug[:500], careers_url[:5000],
            60, 360, next_scan, now,
        )
        return row["id"] if row else None
    except Exception:
        return None


async def main():
    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)
    print(f"Scraping {len(BATCHES)} YC batches via Algolia (free)…\n", flush=True)

    total_companies = 0
    total_inserted = 0
    async with httpx.AsyncClient() as client:
        for batch in BATCHES:
            hits = await algolia_query(client, batch)
            if not hits:
                continue
            inserted = 0
            for h in hits:
                cid = await upsert_company(conn, h)
                if cid:
                    inserted += 1
            print(f"  {batch}: {len(hits):>4} hiring companies → "
                  f"upserted {inserted}", flush=True)
            total_companies += len(hits)
            total_inserted += inserted
            await asyncio.sleep(0.3)  # politeness

    await conn.close()
    print(f"\n=== SUMMARY ===", flush=True)
    print(f"  Total YC companies seen across batches: {total_companies}", flush=True)
    print(f"  Companies upserted:                     {total_inserted}", flush=True)
    print(f"  Cost:                                   $0.00", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
