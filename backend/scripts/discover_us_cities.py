"""
Discover companies in 30 major US tech metros and add them to the
JobJarvis companies index.

Sources (all free, no auth):
  1. Wikidata SPARQL — structured "companies headquartered in <city>"
     queries.  Returns name + official website for every Wikipedia-notable
     company.  ~500-3,000 companies per city.
  2. Built In — curated tech-company directory for 10 of the cities.
     Scrapes their public listing pages.

Both feed into `detect_ats()` to classify the careers URL, then
`upsert_company()` for idempotent insert.  Already-known companies are
skipped via the case-insensitive unique index.

Usage:
  # one-shot run inside the worker container:
  docker cp backend/scripts/discovery_lib.py    jobjarvis_celery_worker:/tmp/
  docker cp backend/scripts/discover_us_cities.py jobjarvis_celery_worker:/tmp/
  docker exec jobjarvis_celery_worker python3 -u /tmp/discover_us_cities.py

  # or restrict to one city for testing:
  docker exec jobjarvis_celery_worker python3 -u /tmp/discover_us_cities.py --city sf

Output:
  /tmp/us_cities_scrape.json — per-city stats (companies found, new, ats-known)
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from discovery_lib import (  # noqa: E402
    detect_ats, slug_to_name, get_db_conn, upsert_company,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("discover_us_cities")


# ───────────────────────────────────────────────────────────────────────────
#  The 30 cities
# ───────────────────────────────────────────────────────────────────────────
#
# wikidata_id = Wikidata Q-number for the city / metro area.  Used to query
#               all companies whose HQ is administratively within it.
# builtin_slug = URL slug on builtin.com if they have a city page, else None.

CITIES: dict[str, dict] = {
    "sf":          {"display": "San Francisco",       "wikidata_id": "Q62",     "builtin_slug": "san-francisco"},
    "nyc":         {"display": "New York City",       "wikidata_id": "Q60",     "builtin_slug": "new-york"},
    "la":          {"display": "Los Angeles",         "wikidata_id": "Q65",     "builtin_slug": "los-angeles"},
    "seattle":     {"display": "Seattle",             "wikidata_id": "Q5083",   "builtin_slug": "seattle"},
    "austin":      {"display": "Austin",              "wikidata_id": "Q16559",  "builtin_slug": "austin"},
    "boston":      {"display": "Boston",              "wikidata_id": "Q100",    "builtin_slug": "boston"},
    "chicago":     {"display": "Chicago",             "wikidata_id": "Q1297",   "builtin_slug": "chicago"},
    "dc":          {"display": "Washington DC",       "wikidata_id": "Q61",     "builtin_slug": "washington-dc"},
    "atlanta":     {"display": "Atlanta",             "wikidata_id": "Q23556",  "builtin_slug": None},
    "denver":      {"display": "Denver",              "wikidata_id": "Q16554",  "builtin_slug": "colorado"},   # Built In Colorado covers Denver + Boulder
    "boulder":     {"display": "Boulder",             "wikidata_id": "Q201375", "builtin_slug": None},
    "san_diego":   {"display": "San Diego",           "wikidata_id": "Q16552",  "builtin_slug": None},
    "dallas":      {"display": "Dallas",              "wikidata_id": "Q16557",  "builtin_slug": None},
    "houston":     {"display": "Houston",             "wikidata_id": "Q16555",  "builtin_slug": None},
    "miami":       {"display": "Miami",               "wikidata_id": "Q8652",   "builtin_slug": None},
    "philly":      {"display": "Philadelphia",        "wikidata_id": "Q1345",   "builtin_slug": None},
    "portland":    {"display": "Portland, OR",        "wikidata_id": "Q6106",   "builtin_slug": None},
    "minneapolis": {"display": "Minneapolis",         "wikidata_id": "Q36091",  "builtin_slug": None},
    "phoenix":     {"display": "Phoenix",             "wikidata_id": "Q16556",  "builtin_slug": None},
    "pittsburgh":  {"display": "Pittsburgh",          "wikidata_id": "Q1342",   "builtin_slug": None},
    "raleigh":     {"display": "Raleigh-Durham",      "wikidata_id": "Q49255",  "builtin_slug": None},
    "slc":         {"display": "Salt Lake City",      "wikidata_id": "Q23337",  "builtin_slug": None},
    "nashville":   {"display": "Nashville",           "wikidata_id": "Q23197",  "builtin_slug": None},
    "charlotte":   {"display": "Charlotte",           "wikidata_id": "Q16565",  "builtin_slug": None},
    "detroit":     {"display": "Detroit / Ann Arbor", "wikidata_id": "Q12439",  "builtin_slug": None},
    "tampa":       {"display": "Tampa",               "wikidata_id": "Q49255",  "builtin_slug": None},
    "indy":        {"display": "Indianapolis",        "wikidata_id": "Q6346",   "builtin_slug": None},
    "columbus":    {"display": "Columbus",            "wikidata_id": "Q16567",  "builtin_slug": None},
    "kc":          {"display": "Kansas City",         "wikidata_id": "Q41819",  "builtin_slug": None},
    "stl":         {"display": "St. Louis",           "wikidata_id": "Q38022",  "builtin_slug": None},
}


# ───────────────────────────────────────────────────────────────────────────
#  Source #1 — Wikidata SPARQL
# ───────────────────────────────────────────────────────────────────────────

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Returns every entity that:
#   - is an instance/subclass of "business" (Q4830453) OR "organization" (Q43229)
#   - has headquarters location whose administrative chain includes <city>
#   - optionally has an official website (P856)
WIKIDATA_QUERY = """
SELECT DISTINCT ?company ?companyLabel ?website WHERE {
  ?company wdt:P31/wdt:P279* wd:Q4830453 .
  ?company wdt:P159 ?hq .
  ?hq wdt:P131* wd:%(city_id)s .
  OPTIONAL { ?company wdt:P856 ?website . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 3000
"""


async def fetch_wikidata_companies(
    client: httpx.AsyncClient, city_key: str, city: dict
) -> list[dict]:
    """SPARQL query → list of {name, website}.  Robust to timeouts."""
    q = WIKIDATA_QUERY % {"city_id": city["wikidata_id"]}
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": "JobJarvis-CityDiscovery/1.0 (jobjarvis.local)",
    }
    try:
        r = await client.get(
            WIKIDATA_SPARQL_ENDPOINT,
            params={"query": q},
            headers=headers,
            timeout=60.0,
        )
        if r.status_code != 200:
            log.warning("wikidata %s status=%s body=%s", city_key, r.status_code, r.text[:200])
            return []
        data = r.json()
    except Exception as e:
        log.warning("wikidata %s failed: %s", city_key, e)
        return []

    rows = data.get("results", {}).get("bindings", [])
    out: list[dict] = []
    for row in rows:
        name = (row.get("companyLabel") or {}).get("value", "").strip()
        website = (row.get("website") or {}).get("value", "").strip()
        if not name:
            continue
        # Skip Wikidata Q-id placeholders (when label is missing)
        if re.match(r"^Q\d+$", name):
            continue
        out.append({"name": name, "website": website})
    log.info("wikidata %s → %d companies", city_key, len(out))
    return out


# ───────────────────────────────────────────────────────────────────────────
#  Source #2 — Built In
# ───────────────────────────────────────────────────────────────────────────

BUILTIN_BASE = "https://builtin.com"
BUILTIN_PAGE_SIZE = 25  # 25 companies per page

# Built In renders SSR'd HTML.  Each company link has the shape:
#   <a href="/company/<company-slug>" ...>
# And the actual careers/job-board URL is two pages deeper.  For the bulk
# discovery, just grabbing the Built In company page is enough because the
# Built In page itself almost always links to the company's official site /
# careers page in a recognizable spot.

BUILTIN_COMPANY_LINK_RE = re.compile(
    r'href="(/company/[a-z0-9][\w-]+)"', re.I
)


async def fetch_builtin_companies(
    client: httpx.AsyncClient, city_key: str, city: dict
) -> list[dict]:
    """Walk Built In's paginated city listing pages."""
    slug = city.get("builtin_slug")
    if not slug:
        return []

    discovered: set[str] = set()
    out: list[dict] = []

    for page in range(0, 80):  # 80 pages * 25 = up to 2,000 companies / city
        url = f"{BUILTIN_BASE}/companies/{slug}"
        if page > 0:
            url += f"?page={page}"
        try:
            r = await client.get(url, timeout=30.0)
            if r.status_code != 200:
                log.info("builtin %s page=%d status=%s, stopping", city_key, page, r.status_code)
                break
        except Exception as e:
            log.warning("builtin %s page=%d failed: %s", city_key, page, e)
            break

        new_on_page = 0
        for m in BUILTIN_COMPANY_LINK_RE.finditer(r.text):
            href = m.group(1)
            if href in discovered:
                continue
            discovered.add(href)
            new_on_page += 1
            # Derive name from slug; the Built In page itself has the careers
            # URL but fetching every detail page would be 1000+ requests per
            # city.  For now: capture as Built In company URL; our detect_ats
            # pass will follow + classify if it leads to a known ATS.
            company_name = href.split("/company/", 1)[-1].replace("-", " ").title()
            out.append({
                "name": company_name,
                "website": f"{BUILTIN_BASE}{href}",
            })

        if new_on_page == 0:
            # No new companies on this page — we've hit the end.
            break

        # Politeness: 1 request per second to Built In.
        await asyncio.sleep(1.0)

    log.info("builtin %s → %d companies", city_key, len(out))
    return out


# ───────────────────────────────────────────────────────────────────────────
#  Main pipeline
# ───────────────────────────────────────────────────────────────────────────

async def discover_city(
    client: httpx.AsyncClient, conn, city_key: str, city: dict
) -> dict:
    """Run both sources for one city, upsert results, return stats."""
    t0 = time.time()
    stats = {
        "city": city["display"],
        "wikidata_count": 0,
        "builtin_count":  0,
        "ats_known":      0,
        "new_inserts":    0,
        "elapsed_s":      0.0,
    }

    wiki = await fetch_wikidata_companies(client, city_key, city)
    stats["wikidata_count"] = len(wiki)
    built = await fetch_builtin_companies(client, city_key, city)
    stats["builtin_count"] = len(built)

    seen: set[str] = set()
    for entry in (wiki + built):
        name = entry["name"]
        url = entry.get("website") or ""
        if not url:
            continue
        # Drop www. + trailing slash for dedup
        host = urlparse(url).netloc.lower()
        if host in seen:
            continue
        seen.add(host)

        ats_match = detect_ats(url)
        if ats_match:
            stats["ats_known"] += 1
            ats_type, slug = ats_match
            try:
                cid = await upsert_company(
                    conn, name=name, ats=ats_type, slug=slug, careers_url=url
                )
                if cid:
                    stats["new_inserts"] += 1
            except Exception as e:
                log.debug("upsert failed name=%s err=%s", name, e)
            continue

        # Not a recognized ATS URL.  We still record the company with
        # ats='unknown' and the raw careers/website URL so a later pass can
        # crawl the careers page and try to discover the ATS.
        try:
            cid = await upsert_company(
                conn, name=name, ats="unknown", slug=name.lower().replace(" ", "-")[:60],
                careers_url=url,
            )
            if cid:
                stats["new_inserts"] += 1
        except Exception as e:
            log.debug("upsert unknown name=%s err=%s", name, e)

    stats["elapsed_s"] = round(time.time() - t0, 1)
    log.info(
        "%-22s wikidata=%4d  builtin=%4d  ats_known=%4d  new=%4d  (%.1fs)",
        city["display"],
        stats["wikidata_count"], stats["builtin_count"],
        stats["ats_known"], stats["new_inserts"], stats["elapsed_s"],
    )
    return stats


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--city", default=None,
                   help="Run for one city only (key from CITIES dict)")
    p.add_argument("--out", default="/tmp/us_cities_scrape.json")
    args = p.parse_args()

    if args.city and args.city not in CITIES:
        log.error("Unknown city %s. Available: %s", args.city, ", ".join(CITIES.keys()))
        sys.exit(2)

    targets = (
        {args.city: CITIES[args.city]} if args.city else CITIES
    )

    conn = await get_db_conn()
    all_stats: dict[str, dict] = {}
    grand_total_new = 0

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "JobJarvis-CityDiscovery/1.0"},
    ) as client:
        for key, city in targets.items():
            try:
                stats = await discover_city(client, conn, key, city)
                all_stats[key] = stats
                grand_total_new += stats["new_inserts"]
            except Exception as e:
                log.exception("city %s failed: %s", key, e)
                all_stats[key] = {"city": city["display"], "error": str(e)}

            # Politeness between cities.
            await asyncio.sleep(2.0)

    await conn.close()

    Path(args.out).write_text(json.dumps(all_stats, indent=2))
    log.info("DONE — total new companies added: %d", grand_total_new)
    log.info("per-city report: %s", args.out)


if __name__ == "__main__":
    asyncio.run(main())
