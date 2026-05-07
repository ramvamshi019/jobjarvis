"""
Discover companies from "Ask HN: Who is hiring?" monthly threads.

Strategy:
  1. Use HN's Algolia search API (public, no auth, free, rate-limited politely)
     to find every monthly "Who is hiring" thread from 2018→present.
  2. For each thread, fetch the full comment tree from the items endpoint.
  3. Regex out every URL in every comment.
  4. Classify each URL with discovery_lib.detect_ats().
  5. Upsert into companies.

Adds ~3,000–5,000 unique tech companies in ~10–20 minutes. $0 cost.

Usage:
  docker cp backend/scripts/discovery_lib.py jobjarvis_celery_worker:/tmp/discovery_lib.py
  docker cp backend/scripts/discover_hn_whoshiring.py jobjarvis_celery_worker:/tmp/
  docker exec jobjarvis_celery_worker python3 -u /tmp/discover_hn_whoshiring.py
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from discovery_lib import (  # noqa: E402
    detect_ats, slug_to_name, get_db_conn, upsert_company,
)


HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ITEM   = "https://hn.algolia.com/api/v1/items/{id}"

# Permissive URL regex — captures most real-world links.
_URL_RE = re.compile(
    r"https?://[\w\-.]+(?:/[\w\-./?=&%#+~]*)?",
    re.I,
)


async def find_who_is_hiring_stories(client: httpx.AsyncClient,
                                     since_year: int = 2019) -> list[int]:
    """Return story IDs of every monthly Who-is-hiring thread."""
    story_ids: set[int] = set()
    # Algolia paginates 100 hits per page; we just walk pages.
    for page in range(0, 30):
        params = {
            "query":       "Ask HN Who is hiring",
            "tags":        "story,author_whoishiring",
            "hitsPerPage": 100,
            "page":        page,
        }
        r = await client.get(HN_SEARCH, params=params, timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        hits = data.get("hits") or []
        if not hits:
            break
        for h in hits:
            title = (h.get("title") or "").lower()
            if "who is hiring" not in title:
                continue
            ts = h.get("created_at_i") or 0
            year = datetime.fromtimestamp(ts, tz=timezone.utc).year
            if year < since_year:
                continue
            sid = h.get("objectID")
            if sid and sid.isdigit():
                story_ids.add(int(sid))
        # Stop early if we've gone past the cutoff
        oldest_ts = min((h.get("created_at_i") or 0) for h in hits)
        if oldest_ts and datetime.fromtimestamp(oldest_ts, tz=timezone.utc).year < since_year:
            break
    return sorted(story_ids, reverse=True)


async def extract_urls_from_thread(client: httpx.AsyncClient,
                                   story_id: int) -> list[str]:
    """Walk a thread and pull every URL from every comment."""
    try:
        r = await client.get(HN_ITEM.format(id=story_id), timeout=60)
    except Exception:
        return []
    if r.status_code != 200:
        return []

    urls: list[str] = []
    def walk(node):
        text = node.get("text") or ""
        if text:
            urls.extend(_URL_RE.findall(text))
        for child in node.get("children") or []:
            walk(child)

    walk(r.json())
    return urls


async def main():
    print(f"Connecting to DB…", flush=True)
    conn = await get_db_conn()

    print("Fetching list of HN 'Who is hiring' stories…", flush=True)
    async with httpx.AsyncClient(headers={"User-Agent": "JobJarvis/1.0"}) as client:
        story_ids = await find_who_is_hiring_stories(client, since_year=2019)
        print(f"  found {len(story_ids)} hiring threads (2019→now)\n", flush=True)

        seen_pairs: set[tuple[str, str]] = set()
        new_companies = 0
        skipped = 0

        for i, sid in enumerate(story_ids, 1):
            urls = await extract_urls_from_thread(client, sid)
            if not urls:
                continue

            # Classify and dedupe within this thread
            thread_pairs = set()
            for url in urls:
                hit = detect_ats(url)
                if hit:
                    thread_pairs.add(hit + (url,))

            inserted = 0
            for ats_type, slug, src_url in thread_pairs:
                key = (ats_type, slug)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                cid = await upsert_company(
                    conn,
                    name=slug_to_name(slug),
                    ats=ats_type,
                    slug=slug,
                    careers_url=src_url,
                )
                if cid:
                    inserted += 1
                else:
                    skipped += 1

            new_companies += inserted
            print(f"  [{i:>3}/{len(story_ids)}] story {sid}: "
                  f"{len(urls):>4} urls, {len(thread_pairs):>3} ATS hits, "
                  f"+{inserted:>3} new (running total: {new_companies})",
                  flush=True)
            await asyncio.sleep(0.4)  # politeness toward HN/Algolia

    print(f"\n=== HN DISCOVERY DONE ===", flush=True)
    print(f"  New/updated companies: {new_companies}", flush=True)
    print(f"  Skipped (errors/dupes): {skipped}", flush=True)

    counts = await conn.fetch(
        "SELECT ats, COUNT(*) AS n FROM companies WHERE active=true "
        "GROUP BY ats ORDER BY n DESC"
    )
    print("\n  Updated company breakdown by ATS:", flush=True)
    for r in counts:
        print(f"    {r['ats']:18s}  {r['n']:>6}", flush=True)

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
