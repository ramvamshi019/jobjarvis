"""Custom portal connector — scrapes company career pages that don't use standard ATS.

Strategy (in order):
  1. Sitemap walk  — parse /sitemap.xml or /careers/sitemap.xml → job URLs
  2. JSON-LD       — extract schema.org/JobPosting blocks from each page
  3. HTML fallback — look for common job-list patterns in the page HTML

ats_identifier format:  "https://jobs.apple.com"  (the career portal root URL)
"""
import asyncio
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

MAX_JOBS_PER_COMPANY = 500   # cap to avoid runaway scrapes
SITEMAP_TIMEOUT = 15
PAGE_TIMEOUT = 10
CONCURRENCY = 8              # parallel page fetches


class CustomPortalConnector(BaseConnector):
    """Scrapes custom career portals using JSON-LD structured data."""

    ats_type = "custom_portal"

    def _default_headers(self) -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (compatible; JobJarvis/1.0; "
                "+https://jobjarvis.ai/bot)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        """ats_identifier = career portal root URL, e.g. 'https://jobs.apple.com'"""
        start = time.monotonic()
        base_url = ats_identifier.rstrip("/")
        domain = urlparse(base_url).netloc

        try:
            async with httpx.AsyncClient(
                headers=self._default_headers(),
                follow_redirects=True,
                timeout=httpx.Timeout(SITEMAP_TIMEOUT),
            ) as client:
                # Step 1: try to get job URLs from sitemap
                job_urls = await self._collect_job_urls(client, base_url)

                if not job_urls:
                    # Step 2: try the main careers page itself (JSON-LD direct)
                    jobs = await self._scrape_page(client, base_url, company_id)
                    elapsed = int((time.monotonic() - start) * 1000)
                    return ConnectorResult(
                        company_id=company_id,
                        ats_type=self.ats_type,
                        jobs=jobs,
                        success=True,
                        response_time_ms=elapsed,
                    )

                # Step 3: scrape each job URL in parallel (batched)
                sem = asyncio.Semaphore(CONCURRENCY)
                jobs: list[RawJob] = []

                async def _fetch_one(url: str):
                    async with sem:
                        return await self._scrape_page(client, url, company_id)

                results = await asyncio.gather(
                    *[_fetch_one(u) for u in job_urls[:MAX_JOBS_PER_COMPANY]],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, list):
                        jobs.extend(r)

                elapsed = int((time.monotonic() - start) * 1000)
                logger.info(
                    "custom_portal_fetched",
                    company_id=company_id,
                    portal=base_url,
                    urls_found=len(job_urls),
                    jobs_parsed=len(jobs),
                )
                return ConnectorResult(
                    company_id=company_id,
                    ats_type=self.ats_type,
                    jobs=jobs,
                    success=True,
                    response_time_ms=elapsed,
                )

        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.warning("custom_portal_failed", portal=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id,
                ats_type=self.ats_type,
                success=False,
                error=str(e),
                response_time_ms=elapsed,
            )

    # ── Sitemap walker ────────────────────────────────────────────────────────

    async def _collect_job_urls(
        self, client: httpx.AsyncClient, base_url: str
    ) -> list[str]:
        """Walk sitemap(s) and return URLs that look like individual job pages."""
        sitemap_candidates = [
            f"{base_url}/sitemap.xml",
            f"{base_url}/sitemap_index.xml",
            f"{base_url}/careers/sitemap.xml",
            f"{base_url}/jobs/sitemap.xml",
        ]
        for sitemap_url in sitemap_candidates:
            try:
                r = await client.get(sitemap_url, timeout=SITEMAP_TIMEOUT)
                if r.status_code != 200:
                    continue
                urls = self._parse_sitemap(r.text, base_url)
                if urls:
                    logger.info("custom_portal_sitemap", url=sitemap_url, count=len(urls))
                    return urls
            except Exception:
                continue
        return []

    def _parse_sitemap(self, xml_text: str, base_url: str) -> list[str]:
        """Extract job-looking URLs from a sitemap XML."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        job_patterns = re.compile(
            r"/jobs?/|/careers?/|/openings?/|/positions?/|/vacancies?/|\d{5,}",
            re.IGNORECASE,
        )

        urls = []
        # Could be sitemap index → recurse is handled below
        for loc in root.findall(".//sm:loc", ns):
            url = (loc.text or "").strip()
            if url and job_patterns.search(url):
                urls.append(url)

        return urls[:MAX_JOBS_PER_COMPANY]

    # ── JSON-LD page scraper ──────────────────────────────────────────────────

    async def _scrape_page(
        self, client: httpx.AsyncClient, url: str, company_id: int
    ) -> list[RawJob]:
        """Fetch one page and extract all JobPosting JSON-LD blocks."""
        try:
            r = await client.get(url, timeout=PAGE_TIMEOUT)
            if r.status_code != 200:
                return []
            return self._extract_jsonld_jobs(r.text, url)
        except Exception:
            return []

    def _extract_jsonld_jobs(self, html: str, page_url: str) -> list[RawJob]:
        """Parse schema.org/JobPosting blocks from raw HTML."""
        jobs = []
        # Find all <script type="application/ld+json"> blocks
        blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        for block in blocks:
            try:
                data = json.loads(block.strip())
            except (json.JSONDecodeError, ValueError):
                continue

            # Could be a single object or a @graph array
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                graph = data.get("@graph", [])
                if graph:
                    items = graph
                else:
                    items = [data]

            for item in items:
                if not isinstance(item, dict):
                    continue
                job_type = item.get("@type", "")
                if isinstance(job_type, list):
                    job_type = job_type[0] if job_type else ""
                if job_type != "JobPosting":
                    continue
                job = self._parse_jsonld_item(item, page_url)
                if job:
                    jobs.append(job)

        return jobs

    def _parse_jsonld_item(self, item: dict, page_url: str) -> Optional[RawJob]:
        title = item.get("title", item.get("name", "")).strip()
        if not title:
            return None

        url = item.get("url", item.get("sameAs", page_url))
        description = item.get("description", "")
        company_name = ""
        hiringOrg = item.get("hiringOrganization", {})
        if isinstance(hiringOrg, dict):
            company_name = hiringOrg.get("name", "")

        # Location
        location_obj = item.get("jobLocation", {})
        location = ""
        if isinstance(location_obj, dict):
            addr = location_obj.get("address", {})
            if isinstance(addr, dict):
                parts = [
                    addr.get("addressLocality", ""),
                    addr.get("addressRegion", ""),
                    addr.get("addressCountry", ""),
                ]
                location = ", ".join(p for p in parts if p)
            elif isinstance(addr, str):
                location = addr
        elif isinstance(location_obj, str):
            location = location_obj

        # Remote
        remote = False
        job_location_type = item.get("jobLocationType", "")
        if job_location_type == "TELECOMMUTE":
            remote = True
            if not location:
                location = "Remote"

        # Employment type
        emp_type = item.get("employmentType", "")
        if isinstance(emp_type, list):
            emp_type = emp_type[0] if emp_type else ""

        # Salary
        salary_min = salary_max = None
        salary_currency = "USD"
        base_salary = item.get("baseSalary", {})
        if isinstance(base_salary, dict):
            value = base_salary.get("value", {})
            salary_currency = base_salary.get("currency", "USD")
            if isinstance(value, dict):
                salary_min = value.get("minValue")
                salary_max = value.get("maxValue")
                if not salary_min:
                    salary_min = value.get("value")
            elif isinstance(value, (int, float)):
                salary_min = salary_max = int(value)

        # Posted date
        posted_at = None
        date_str = item.get("datePosted", "")
        if date_str:
            try:
                posted_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except Exception:
                pass

        # External ID from URL or identifier
        ext_id = item.get("identifier", {})
        if isinstance(ext_id, dict):
            ext_id = ext_id.get("value", "")
        if not ext_id:
            ext_id = hashlib.sha256(f"{title}|{url}".encode()).hexdigest()[:16]

        return RawJob(
            external_id=str(ext_id),
            title=title,
            company_name=company_name,
            location=location,
            job_url=url,
            apply_url=url,
            description=description,
            employment_type=emp_type.upper() if emp_type else "",
            posted_at=posted_at,
            salary_min=int(salary_min) if salary_min else None,
            salary_max=int(salary_max) if salary_max else None,
            salary_currency=salary_currency,
            remote=remote,
            source="custom_portal",
            raw_json=item,
        )

    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        """Required by BaseConnector but we override fetch_jobs entirely."""
        return None
