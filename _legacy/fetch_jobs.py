"""
Multi-Source Async Job Fetch Engine.

Fetches jobs from:
  - Greenhouse API (public board API)
  - Lever API (public postings API)
  - Ashby API (public job board API)
  - Workday (career site scraping)
  - Indeed (RSS feed parsing)
  - LinkedIn (public jobs page — requires LINKEDIN_ENABLED=true)

Features:
  - Token-bucket rate limiter
  - Exponential backoff with jitter
  - Circuit breaker per source
  - Semaphore-based concurrency control
  - Structured logging
"""

import asyncio
import hashlib
import logging
import random
import re
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import quote_plus

import httpx

from config import (
    GREENHOUSE_BOARDS,
    LEVER_BOARDS,
    ASHBY_BOARDS,
    WORKDAY_TENANTS,
    INDEED_SEARCHES,
    LINKEDIN_ENABLED,
    LINKEDIN_SEARCHES,
    JOB_FETCH_SOURCES,
    MAX_CONCURRENCY,
    MAX_RETRIES,
    RATE_LIMIT_PER_SECOND,
    REQUEST_TIMEOUT,
    CIRCUIT_BREAKER_THRESHOLD,
    CIRCUIT_BREAKER_TIMEOUT,
)

logger = logging.getLogger(__name__)


# ─── Rate Limiter ───────────────────────────────────────────────

class RateLimiter:
    """Token-bucket rate limiter for async requests."""

    def __init__(self, rate: float):
        self.rate = max(rate, 0.1)
        self.tokens = rate
        self.max_tokens = rate
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0
            else:
                self.tokens -= 1


# ─── Circuit Breaker ───────────────────────────────────────────

class CircuitBreaker:
    """Per-source circuit breaker to stop hammering failing APIs."""

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD,
                 timeout: int = CIRCUIT_BREAKER_TIMEOUT):
        self.threshold = threshold
        self.timeout = timeout
        self._failures: dict[str, int] = defaultdict(int)
        self._open_until: dict[str, float] = {}

    def is_open(self, source: str) -> bool:
        if source in self._open_until:
            if time.monotonic() < self._open_until[source]:
                return True
            # Reset after timeout
            del self._open_until[source]
            self._failures[source] = 0
        return False

    def record_failure(self, source: str):
        self._failures[source] += 1
        if self._failures[source] >= self.threshold:
            self._open_until[source] = time.monotonic() + self.timeout
            logger.warning(
                f"Circuit breaker OPEN for {source} — "
                f"{self._failures[source]} consecutive failures"
            )

    def record_success(self, source: str):
        self._failures[source] = 0
        self._open_until.pop(source, None)


rate_limiter = RateLimiter(RATE_LIMIT_PER_SECOND)
circuit_breaker = CircuitBreaker()


# ─── Fetch Helpers ──────────────────────────────────────────────

async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    source: str = "unknown",
    retries: int = MAX_RETRIES,
    parse_json: bool = True,
) -> Optional[dict | list | str]:
    """Fetch a URL with exponential backoff + jitter retry."""
    if circuit_breaker.is_open(source):
        logger.debug(f"Circuit breaker open for {source}, skipping {url}")
        return None

    for attempt in range(retries):
        await rate_limiter.acquire()
        try:
            resp = await client.get(url, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                jitter = random.uniform(0, 1)
                wait = retry_after + jitter
                logger.warning(f"Rate limited on {source} ({url}), waiting {wait:.1f}s")
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            circuit_breaker.record_success(source)

            if parse_json:
                return resp.json()
            return resp.text

        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Server error {e.response.status_code} on {source}, retry in {wait:.1f}s")
                await asyncio.sleep(wait)
            else:
                logger.error(f"HTTP {e.response.status_code} for {source}: {url}")
                circuit_breaker.record_failure(source)
                return None

        except (httpx.RequestError, httpx.TimeoutException) as e:
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Request error on {source}: {e}, retry in {wait:.1f}s")
            await asyncio.sleep(wait)

    logger.error(f"All {retries} retries exhausted for {source}: {url}")
    circuit_breaker.record_failure(source)
    return None


def make_job_id(source: str, company: str, external_id: str) -> str:
    raw = f"{source}:{company}:{external_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def clean_html(text: str) -> str:
    """Strip HTML tags from text."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


# ═══════════════════════════════════════════════════════════════
# SOURCE: Greenhouse
# ═══════════════════════════════════════════════════════════════

async def fetch_greenhouse_board(
    client: httpx.AsyncClient, board: str, semaphore: asyncio.Semaphore
) -> list[dict]:
    """Fetch all jobs from a Greenhouse board."""
    async with semaphore:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        data = await fetch_with_retry(client, url, source=f"greenhouse:{board}")
        if not data or "jobs" not in data:
            return []

        jobs = []
        for j in data["jobs"]:
            loc_name = ""
            if j.get("location"):
                loc_name = j["location"].get("name", "")

            jobs.append({
                "job_id": make_job_id("greenhouse", board, str(j["id"])),
                "source": "greenhouse",
                "company": board.replace("-", " ").title(),
                "title": j.get("title", ""),
                "description": j.get("content", ""),
                "location": loc_name,
                "link": j.get("absolute_url", ""),
            })
        logger.info(f"[Greenhouse] {board}: {len(jobs)} jobs")
        return jobs


async def fetch_all_greenhouse(client: httpx.AsyncClient) -> list[dict]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [fetch_greenhouse_board(client, b, semaphore) for b in GREENHOUSE_BOARDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_jobs = []
    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"Greenhouse error: {r}")
    logger.info(f"[Greenhouse] Total: {len(all_jobs)} jobs from {len(GREENHOUSE_BOARDS)} boards")
    return all_jobs


# ═══════════════════════════════════════════════════════════════
# SOURCE: Lever
# ═══════════════════════════════════════════════════════════════

async def fetch_lever_board(
    client: httpx.AsyncClient, company: str, semaphore: asyncio.Semaphore
) -> list[dict]:
    """Fetch all jobs from a Lever postings API."""
    async with semaphore:
        url = f"https://api.lever.co/v0/postings/{company}?mode=json"
        data = await fetch_with_retry(client, url, source=f"lever:{company}")
        if not data or not isinstance(data, list):
            return []

        jobs = []
        for j in data:
            loc = ""
            if j.get("categories", {}).get("location"):
                loc = j["categories"]["location"]

            description = j.get("descriptionPlain", "") or j.get("description", "")
            lists_text = ""
            for lst in j.get("lists", []):
                lists_text += f"\n{lst.get('text', '')}\n{lst.get('content', '')}"

            jobs.append({
                "job_id": make_job_id("lever", company, j.get("id", "")),
                "source": "lever",
                "company": company.replace("-", " ").title(),
                "title": j.get("text", ""),
                "description": (description + lists_text)[:10000],
                "location": loc,
                "link": j.get("hostedUrl", "") or j.get("applyUrl", ""),
            })
        logger.info(f"[Lever] {company}: {len(jobs)} jobs")
        return jobs


async def fetch_all_lever(client: httpx.AsyncClient) -> list[dict]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [fetch_lever_board(client, c, semaphore) for c in LEVER_BOARDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_jobs = []
    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"Lever error: {r}")
    logger.info(f"[Lever] Total: {len(all_jobs)} jobs from {len(LEVER_BOARDS)} boards")
    return all_jobs


# ═══════════════════════════════════════════════════════════════
# SOURCE: Ashby
# ═══════════════════════════════════════════════════════════════

async def fetch_ashby_board(
    client: httpx.AsyncClient, company: str, semaphore: asyncio.Semaphore
) -> list[dict]:
    """Fetch jobs from Ashby's public job board API."""
    async with semaphore:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
        data = await fetch_with_retry(client, url, source=f"ashby:{company}")
        if not data or "jobs" not in data:
            return []

        jobs = []
        for j in data["jobs"]:
            loc = j.get("location", "")
            if isinstance(loc, dict):
                loc = loc.get("name", "")

            desc = j.get("descriptionHtml", "") or j.get("descriptionPlain", "")

            jobs.append({
                "job_id": make_job_id("ashby", company, j.get("id", "")),
                "source": "ashby",
                "company": company.replace("-", " ").title(),
                "title": j.get("title", ""),
                "description": desc[:10000],
                "location": loc if isinstance(loc, str) else "",
                "link": j.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{company}/{j.get('id', '')}",
            })
        logger.info(f"[Ashby] {company}: {len(jobs)} jobs")
        return jobs


async def fetch_all_ashby(client: httpx.AsyncClient) -> list[dict]:
    if not ASHBY_BOARDS:
        return []
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [fetch_ashby_board(client, c, semaphore) for c in ASHBY_BOARDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_jobs = []
    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"Ashby error: {r}")
    logger.info(f"[Ashby] Total: {len(all_jobs)} jobs from {len(ASHBY_BOARDS)} boards")
    return all_jobs


# ═══════════════════════════════════════════════════════════════
# SOURCE: Workday
# ═══════════════════════════════════════════════════════════════

async def fetch_workday_tenant(
    client: httpx.AsyncClient, tenant_config: str, semaphore: asyncio.Semaphore
) -> list[dict]:
    """
    Fetch jobs from a Workday career site.
    tenant_config format: "company_name:tenant_subdomain"

    Workday's public search API is commonly at:
    https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    """
    async with semaphore:
        parts = tenant_config.split(":", 1)
        if len(parts) != 2:
            logger.warning(f"Invalid Workday config: {tenant_config}")
            return []

        company_name, tenant = parts

        # Try common Workday API patterns
        api_urls = [
            f"https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/External/jobs",
            f"https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/External/jobs",
            f"https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/en-US/External/jobs",
        ]

        payload = {
            "appliedFacets": {},
            "limit": 20,
            "offset": 0,
            "searchText": "",
        }

        for url in api_urls:
            try:
                await rate_limiter.acquire()
                resp = await client.post(url, json=payload, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    job_postings = data.get("jobPostings", [])
                    jobs = []
                    for j in job_postings:
                        title = j.get("title", "")
                        loc = j.get("locationsText", "") or j.get("location", "")
                        ext_path = j.get("externalPath", "")
                        link = f"https://{tenant}.wd5.myworkdayjobs.com{ext_path}" if ext_path else ""

                        jobs.append({
                            "job_id": make_job_id("workday", company_name, j.get("bulletFields", [str(hash(title))])[0] if j.get("bulletFields") else str(hash(title + loc))),
                            "source": "workday",
                            "company": company_name.replace("-", " ").title(),
                            "title": title,
                            "description": j.get("descriptionText", ""),
                            "location": loc,
                            "link": link,
                        })
                    logger.info(f"[Workday] {company_name}: {len(jobs)} jobs")
                    circuit_breaker.record_success(f"workday:{company_name}")
                    return jobs
            except Exception as e:
                logger.debug(f"Workday URL failed for {company_name}: {e}")
                continue

        logger.warning(f"[Workday] Could not fetch jobs for {company_name}")
        circuit_breaker.record_failure(f"workday:{company_name}")
        return []


async def fetch_all_workday(client: httpx.AsyncClient) -> list[dict]:
    if not WORKDAY_TENANTS:
        return []
    semaphore = asyncio.Semaphore(min(MAX_CONCURRENCY, 5))  # Lower concurrency for Workday
    tasks = [fetch_workday_tenant(client, t, semaphore) for t in WORKDAY_TENANTS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_jobs = []
    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"Workday error: {r}")
    logger.info(f"[Workday] Total: {len(all_jobs)} jobs from {len(WORKDAY_TENANTS)} tenants")
    return all_jobs


# ═══════════════════════════════════════════════════════════════
# SOURCE: Indeed (RSS Feeds)
# ═══════════════════════════════════════════════════════════════

async def fetch_indeed_search(
    client: httpx.AsyncClient, search: dict, semaphore: asyncio.Semaphore
) -> list[dict]:
    """Fetch jobs from Indeed's RSS feed."""
    async with semaphore:
        query = quote_plus(search.get("query", ""))
        location = quote_plus(search.get("location", ""))
        url = f"https://www.indeed.com/rss?q={query}&l={location}&sort=date&limit=25"

        text = await fetch_with_retry(
            client, url, source="indeed", parse_json=False
        )
        if not text:
            return []

        # Simple XML parsing for RSS (avoid heavy XML dependency)
        jobs = []
        items = re.findall(r"<item>(.*?)</item>", text, re.DOTALL)
        for item in items:
            title_match = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item)
            if not title_match:
                title_match = re.search(r"<title>(.*?)</title>", item)
            title = clean_html(title_match.group(1)) if title_match else ""

            link_match = re.search(r"<link>(.*?)</link>", item)
            link = link_match.group(1).strip() if link_match else ""

            desc_match = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", item, re.DOTALL)
            if not desc_match:
                desc_match = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
            description = clean_html(desc_match.group(1)) if desc_match else ""

            # Extract company from title pattern: "Job Title - Company Name"
            company = "Unknown"
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                if len(parts) == 2:
                    title, company = parts[0].strip(), parts[1].strip()

            source_match = re.search(r"<source.*?>(.*?)</source>", item)
            if source_match:
                company = clean_html(source_match.group(1))

            if title and link:
                jobs.append({
                    "job_id": make_job_id("indeed", company, link),
                    "source": "indeed",
                    "company": company,
                    "title": title,
                    "description": description[:10000],
                    "location": search.get("location", ""),
                    "link": link,
                })

        logger.info(f"[Indeed] '{search.get('query')}': {len(jobs)} jobs")
        return jobs


async def fetch_all_indeed(client: httpx.AsyncClient) -> list[dict]:
    if not INDEED_SEARCHES:
        return []
    semaphore = asyncio.Semaphore(min(MAX_CONCURRENCY, 3))
    tasks = [fetch_indeed_search(client, s, semaphore) for s in INDEED_SEARCHES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_jobs = []
    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"Indeed error: {r}")
    logger.info(f"[Indeed] Total: {len(all_jobs)} jobs from {len(INDEED_SEARCHES)} searches")
    return all_jobs


# ═══════════════════════════════════════════════════════════════
# SOURCE: LinkedIn (Public Job Search — opt-in)
# ═══════════════════════════════════════════════════════════════

async def fetch_linkedin_search(
    client: httpx.AsyncClient, search: dict, semaphore: asyncio.Semaphore
) -> list[dict]:
    """
    Fetch jobs from LinkedIn's public job search page.
    Note: LinkedIn aggressively rate-limits. Use cautiously.
    """
    async with semaphore:
        keywords = quote_plus(search.get("keywords", ""))
        location = quote_plus(search.get("location", ""))
        f_wt = search.get("f_WT", "")

        url = (
            f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
            f"keywords={keywords}&location={location}&f_WT={f_wt}"
            f"&start=0&sortBy=DD"
        )

        text = await fetch_with_retry(
            client, url, source="linkedin", parse_json=False
        )
        if not text:
            return []

        jobs = []
        # Parse the HTML response for job cards
        # LinkedIn returns HTML fragments with job-search-card divs
        cards = re.findall(
            r'<div class="base-card.*?</div>\s*</div>\s*</div>',
            text, re.DOTALL
        )

        for card in cards[:25]:  # Limit per search
            title_match = re.search(r'class="base-search-card__title"[^>]*>(.*?)</[^>]+>', card, re.DOTALL)
            company_match = re.search(r'class="hidden-nested-link"[^>]*>(.*?)</[^>]+>', card, re.DOTALL)
            location_match = re.search(r'class="job-search-card__location"[^>]*>(.*?)</[^>]+>', card, re.DOTALL)
            link_match = re.search(r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)', card)

            title = clean_html(title_match.group(1)) if title_match else ""
            company = clean_html(company_match.group(1)) if company_match else "Unknown"
            loc = clean_html(location_match.group(1)) if location_match else ""
            link = link_match.group(1) if link_match else ""

            if title and link:
                jobs.append({
                    "job_id": make_job_id("linkedin", company, link),
                    "source": "linkedin",
                    "company": company,
                    "title": title,
                    "description": "",  # Full description requires visiting each page
                    "location": loc,
                    "link": link,
                })

        logger.info(f"[LinkedIn] '{search.get('keywords')}': {len(jobs)} jobs")
        return jobs


async def fetch_all_linkedin(client: httpx.AsyncClient) -> list[dict]:
    if not LINKEDIN_ENABLED or not LINKEDIN_SEARCHES:
        logger.info("[LinkedIn] Disabled or no searches configured")
        return []
    semaphore = asyncio.Semaphore(2)  # Very conservative for LinkedIn
    tasks = [fetch_linkedin_search(client, s, semaphore) for s in LINKEDIN_SEARCHES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_jobs = []
    for r in results:
        if isinstance(r, list):
            all_jobs.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"LinkedIn error: {r}")
    logger.info(f"[LinkedIn] Total: {len(all_jobs)} jobs")
    return all_jobs


# ═══════════════════════════════════════════════════════════════
# Main Orchestrator
# ═══════════════════════════════════════════════════════════════

async def fetch_all_jobs() -> list[dict]:
    """Fetch jobs from configured sources concurrently (see JOB_FETCH_SOURCES)."""
    logger.info("=" * 60)
    logger.info("Starting multi-source job fetch...")
    logger.info(f"Enabled sources: {', '.join(sorted(JOB_FETCH_SOURCES)) or '(none)'}")
    logger.info("=" * 60)

    start = time.monotonic()

    source_plan: list[tuple[str, str]] = [
        ("greenhouse", "Greenhouse"),
        ("lever", "Lever"),
        ("ashby", "Ashby"),
        ("workday", "Workday"),
        ("indeed", "Indeed"),
        ("linkedin", "LinkedIn"),
    ]

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        tasks = []
        labels: list[str] = []
        fetchers = {
            "greenhouse": lambda: fetch_all_greenhouse(client),
            "lever": lambda: fetch_all_lever(client),
            "ashby": lambda: fetch_all_ashby(client),
            "workday": lambda: fetch_all_workday(client),
            "indeed": lambda: fetch_all_indeed(client),
            "linkedin": lambda: fetch_all_linkedin(client),
        }
        for key, label in source_plan:
            if key in JOB_FETCH_SOURCES and key in fetchers:
                tasks.append(fetchers[key]())
                labels.append(label)

        if not tasks:
            logger.warning(
                "No fetch sources enabled. Set JOB_FETCH_SOURCES "
                "(e.g. greenhouse,lever,ashby,workday,indeed,linkedin)."
            )
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_jobs = []
    source_counts: dict[str, int] = {}

    for name, result in zip(labels, results):
        if isinstance(result, list):
            all_jobs.extend(result)
            source_counts[name] = len(result)
        elif isinstance(result, Exception):
            logger.error("%s fetch failed: %s", name, result)
            source_counts[name] = 0
        else:
            source_counts[name] = 0

    elapsed = time.monotonic() - start

    logger.info("=" * 60)
    logger.info(f"Fetch complete in {elapsed:.1f}s — {len(all_jobs)} total jobs")
    for name, count in source_counts.items():
        logger.info(f"  {name}: {count} jobs")
    logger.info("=" * 60)

    return all_jobs


def run_fetch() -> list[dict]:
    """Synchronous wrapper for the async fetch pipeline."""
    return asyncio.run(fetch_all_jobs())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    jobs = run_fetch()
    print(f"\nFetched {len(jobs)} jobs total.")
    for j in jobs[:10]:
        print(f"  [{j['source']:12s}] {j['company']:20s}: {j['title']}")
