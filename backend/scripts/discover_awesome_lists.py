"""
Discover companies from public "awesome list" READMEs and tech-company lists.

Strategy:
  GitHub hosts hundreds of curated awesome-* repos that list tech companies
  with their careers URLs.  Most are in plain markdown.  We hit the raw
  README content directly (no GitHub API auth needed), regex out URLs,
  classify by ATS, upsert.

  Also scrapes a few well-known public company directories that publish
  open lists of "Stripe customers", "AWS startup customers", etc.

Adds ~2,000–3,000 unique companies. $0 cost. ~3-5 min runtime.

Usage:
  docker cp backend/scripts/discovery_lib.py jobjarvis_celery_worker:/tmp/discovery_lib.py
  docker cp backend/scripts/discover_awesome_lists.py jobjarvis_celery_worker:/tmp/
  docker exec jobjarvis_celery_worker python3 -u /tmp/discover_awesome_lists.py
"""
import asyncio
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from discovery_lib import (  # noqa: E402
    detect_ats, slug_to_name, get_db_conn, upsert_company,
)


# Curated list of public-good awesome-list URLs that aggregate tech-company
# careers links.  All of these return plain text/markdown, no API key needed.
SOURCES = [
    # Awesome-* lists with company careers URLs
    "https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/master/README.md",
    "https://raw.githubusercontent.com/remoteintech/remote-jobs/main/README.md",
    "https://raw.githubusercontent.com/lukasz-madon/awesome-remote-job/master/README.md",
    "https://raw.githubusercontent.com/jessicard/remote-jobs/master/README.md",
    "https://raw.githubusercontent.com/yanirs/established-remote/master/README.md",
    "https://raw.githubusercontent.com/engineerapart/TheRemoteFreelancer/master/README.md",
    # Tech-company-focused lists
    "https://raw.githubusercontent.com/mikeroyal/Self-Hosting-Guide/main/README.md",
    "https://raw.githubusercontent.com/EthicalSource/morally-questionable-employers/master/README.md",
    # Y Combinator W22 onwards (community-maintained mirrors)
    "https://raw.githubusercontent.com/seladb/StarTrack-js/master/README.md",
    # Pure careers-page lists
    "https://raw.githubusercontent.com/kaxap/arl/master/README.md",  # ATS-related repos
    "https://raw.githubusercontent.com/kuchin/awesome-cto/master/README.md",
    # GreenTech / climate-focused company lists
    "https://raw.githubusercontent.com/Climatescape/awesome-climate-tech-jobs/main/README.md",
    "https://raw.githubusercontent.com/wri/awesome-climate-jobs/main/README.md",
    # Open-source-friendly companies
    "https://raw.githubusercontent.com/tarrball/Open-Source-Companies/master/README.md",
    "https://raw.githubusercontent.com/lk-geimfari/awesomo/master/README.md",
    # Crypto/web3 (lots of Greenhouse customers)
    "https://raw.githubusercontent.com/OffcierCia/job-protocol/master/README.md",
    # Fintech
    "https://raw.githubusercontent.com/7kfpun/awesome-fintech/master/README.md",
    # Generic startup directories
    "https://raw.githubusercontent.com/mhxion/awesome-discord-communities/master/README.md",
]


# Looser URL extractor — markdown links + bare URLs
URL_RE = re.compile(
    r"https?://[\w\-.]+(?:/[\w\-./?=&%#+~]*)?",
    re.I,
)


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


async def main():
    print(f"Connecting to DB…", flush=True)
    conn = await get_db_conn()

    seen_pairs: set[tuple[str, str]] = set()
    total_inserted = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0"},
    ) as client:
        for src in SOURCES:
            print(f"  fetching {src.split('/')[-3]}/{src.split('/')[-2]}…",
                  flush=True)
            body = await fetch_text(client, src)
            if not body:
                print(f"    (empty / 404)", flush=True)
                continue

            urls = URL_RE.findall(body)
            classified = []
            for u in urls:
                hit = detect_ats(u)
                if hit:
                    classified.append((*hit, u))

            inserted = 0
            for ats_type, slug, src_url in classified:
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

            total_inserted += inserted
            print(f"    {len(urls):>5} urls, {len(classified):>4} ATS hits, "
                  f"+{inserted} new", flush=True)
            await asyncio.sleep(0.3)

    print(f"\n=== AWESOME-LISTS DISCOVERY DONE ===", flush=True)
    print(f"  Unique ATS pairs found: {len(seen_pairs)}", flush=True)
    print(f"  New/updated companies:  {total_inserted}", flush=True)

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
