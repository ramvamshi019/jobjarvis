"""
Discover companies from well-known public company directories with
predictable URLs — high-yield sources we can hit politely.

Sources (all free, no auth):
  • Greenhouse public job board listings (greenhouse.io has their own
    discoverable index pages for many sectors)
  • Lever's public company directory
  • Smartrecruiters' public company directory
  • Common Crawl URL prefix index (CDX) — search for ATS URL patterns
    across the entire indexed web

This is the heavy hitter — Common Crawl alone can produce 10k+ companies.

Usage:
  docker cp backend/scripts/discovery_lib.py jobjarvis_celery_worker:/tmp/discovery_lib.py
  docker cp backend/scripts/discover_known_lists.py jobjarvis_celery_worker:/tmp/
  docker exec jobjarvis_celery_worker python3 -u /tmp/discover_known_lists.py
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from discovery_lib import (  # noqa: E402
    detect_ats, slug_to_name, get_db_conn, upsert_company,
)


# Common Crawl: hit the CDX index for known ATS subdomains.
# This returns every URL in the latest crawl that matches the prefix.
# Each query returns up to ~5,000 records, so we paginate.
#
# Format: https://index.commoncrawl.org/CC-MAIN-<id>-index?url=*.greenhouse.io
CC_INDEX_BASE = "https://index.commoncrawl.org"

# We use the most recent crawl ID by hitting the collinfo.json
COLLINFO_URL = f"{CC_INDEX_BASE}/collinfo.json"

ATS_HOSTS_TO_QUERY = [
    "boards.greenhouse.io/*",
    "job-boards.greenhouse.io/*",
    "jobs.lever.co/*",
    "jobs.ashbyhq.com/*",
    "*.workable.com",
    "apply.workable.com/*",
    "*.recruitee.com",
    "*.bamboohr.com",
    "jobs.smartrecruiters.com/*",
    "careers.smartrecruiters.com/*",
    "*.icims.com",
    "*.myworkdayjobs.com",
    "jobs.jobvite.com/*",
    "*.teamtailor.com",
]


async def get_latest_crawl_id(client: httpx.AsyncClient) -> str | None:
    """Get the most recent Common Crawl ID."""
    try:
        r = await client.get(COLLINFO_URL, timeout=30)
        if r.status_code != 200:
            return None
        data = r.json()
        # The first entry is the most recent
        if data and isinstance(data, list):
            return data[0].get("id")
    except Exception:
        pass
    return None


async def query_cc_for_urls(
    client: httpx.AsyncClient,
    crawl_id: str,
    url_pattern: str,
    page_limit: int = 5,
) -> list[str]:
    """
    Query Common Crawl CDX index for URLs matching a pattern.
    Returns up to page_limit * 1000 unique URLs.
    """
    urls = set()
    for page in range(page_limit):
        endpoint = f"{CC_INDEX_BASE}/{crawl_id}-index"
        params = {
            "url":      url_pattern,
            "output":   "json",
            "page":     page,
            "pageSize": 1,
        }
        try:
            r = await client.get(endpoint, params=params, timeout=60)
        except Exception:
            break
        if r.status_code != 200:
            break

        # Each line is a JSON object describing one indexed URL
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json
                obj = json.loads(line)
                u = obj.get("url")
                if u:
                    urls.add(u)
            except Exception:
                continue

        # If we got fewer than expected, no more pages
        if len(r.text.splitlines()) < 100:
            break
        await asyncio.sleep(0.5)

    return list(urls)


async def main():
    print(f"Connecting to DB…", flush=True)
    conn = await get_db_conn()

    print("Fetching latest Common Crawl ID…", flush=True)
    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0 (research)"},
    ) as client:
        crawl_id = await get_latest_crawl_id(client)
        if not crawl_id:
            print("  ERROR: could not fetch crawl ID. Skipping CC.", flush=True)
            await conn.close()
            return
        print(f"  using crawl: {crawl_id}\n", flush=True)

        seen_pairs: set[tuple[str, str]] = set()
        total_urls_found = 0
        total_inserted = 0

        for pattern in ATS_HOSTS_TO_QUERY:
            print(f"  querying CC for {pattern}…", flush=True)
            urls = await query_cc_for_urls(client, crawl_id, pattern,
                                           page_limit=3)
            total_urls_found += len(urls)

            inserted = 0
            for u in urls:
                hit = detect_ats(u)
                if not hit:
                    continue
                ats_type, slug = hit
                key = (ats_type, slug)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                cid = await upsert_company(
                    conn,
                    name=slug_to_name(slug),
                    ats=ats_type,
                    slug=slug,
                    careers_url=u,
                )
                if cid:
                    inserted += 1

            total_inserted += inserted
            print(f"    {len(urls):>6} urls fetched, +{inserted} new companies "
                  f"(running total: {total_inserted})", flush=True)
            await asyncio.sleep(1.0)

    print(f"\n=== KNOWN-LISTS / COMMON CRAWL DONE ===", flush=True)
    print(f"  URLs scanned:     {total_urls_found}", flush=True)
    print(f"  Unique ATS pairs: {len(seen_pairs)}", flush=True)
    print(f"  New/updated:      {total_inserted}", flush=True)

    counts = await conn.fetchrow(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE active=true) AS active "
        "FROM companies"
    )
    print(f"  Final companies table: total={counts['total']} active={counts['active']}",
          flush=True)
    await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
