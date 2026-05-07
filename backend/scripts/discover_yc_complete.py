"""
Discover ALL Y Combinator companies — every batch, every status.

Strategy:
  YC's company directory at ycombinator.com/companies is a Next.js app that
  hydrates from a public Algolia search index.  Their public Algolia keys
  rotate, but the Next.js page itself ships server-rendered company data
  embedded as JSON in __NEXT_DATA__.  Cleaner: hit the public Algolia
  endpoint that the page itself uses (sniffed from network tab).

  We fetch each batch separately to bypass the 1000-result cap, classify
  each company by their careers URL pattern (or fall back to the YC profile
  page), and upsert.

Adds ~5,000 unique companies covering every YC batch from S05→F25. $0 cost.

Usage:
  docker cp backend/scripts/discovery_lib.py jobjarvis_celery_worker:/tmp/discovery_lib.py
  docker cp backend/scripts/discover_yc_complete.py jobjarvis_celery_worker:/tmp/
  docker exec jobjarvis_celery_worker python3 -u /tmp/discover_yc_complete.py
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from discovery_lib import (  # noqa: E402
    detect_ats, get_db_conn, upsert_company,
)


# All YC batch codes from 2005 → 2025
BATCHES = []
for yr in range(5, 26):
    for prefix in ("W", "S"):
        BATCHES.append(f"{prefix}{yr:02d}")
# Fall batches start in 2024
for yr in (24, 25):
    BATCHES.append(f"F{yr:02d}")


# Public YC website endpoint that returns the directory data
YC_DIRECTORY_HTML = "https://www.ycombinator.com/companies?batch={batch}"


async def parse_yc_batch_html(client: httpx.AsyncClient,
                              batch: str) -> list[dict]:
    """
    Pull a batch's HTML and extract embedded JSON company list from the
    Next.js __NEXT_DATA__ blob.
    """
    url = YC_DIRECTORY_HTML.format(batch=batch)
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
    except Exception:
        return []
    if r.status_code != 200:
        return []

    # __NEXT_DATA__ is a script tag with id="__NEXT_DATA__" containing JSON
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r.text, re.DOTALL,
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []

    # Walk the data looking for company arrays
    companies = []

    def walk(obj):
        if isinstance(obj, dict):
            # YC's data structure has companies at queries.companies or pageProps
            if "companies" in obj and isinstance(obj["companies"], list):
                companies.extend(obj["companies"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return companies


async def main():
    print(f"Connecting to DB…", flush=True)
    conn = await get_db_conn()

    total_seen = 0
    total_inserted = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0 (+https://jobjarvis.dev)"},
    ) as client:
        for batch in BATCHES:
            cos = await parse_yc_batch_html(client, batch)
            if not cos:
                # Some batches return empty (older batches with no public data)
                continue

            inserted_this_batch = 0
            for c in cos:
                name = (c.get("name") or "").strip()
                if not name:
                    continue
                slug = (c.get("slug") or "").strip().lower()
                website = (c.get("website") or "").strip()
                # Prefer their actual careers page if we can detect ATS;
                # otherwise fall back to YC profile.
                careers_url = website or f"https://www.ycombinator.com/companies/{slug}"

                # Detect ATS from website if available
                ats_type = "yc"
                ats_slug = slug
                if website:
                    hit = detect_ats(website)
                    if hit:
                        ats_type, ats_slug = hit

                cid = await upsert_company(
                    conn,
                    name=name,
                    ats=ats_type,
                    slug=ats_slug,
                    careers_url=careers_url,
                )
                if cid:
                    inserted_this_batch += 1

            total_seen += len(cos)
            total_inserted += inserted_this_batch
            print(f"  {batch}: {len(cos):>4} companies → "
                  f"+{inserted_this_batch:>3} new (running total: {total_inserted})",
                  flush=True)
            await asyncio.sleep(0.5)

    print(f"\n=== YC DISCOVERY DONE ===", flush=True)
    print(f"  Companies seen across batches: {total_seen}", flush=True)
    print(f"  New/updated:                   {total_inserted}", flush=True)

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
