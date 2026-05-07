"""TeamTailor ATS connector.

TeamTailor is widely used by European and growing US companies.

ats_identifier format: the company slug (subdomain)
  e.g. "northvolt"  →  https://northvolt.teamtailor.com

Public API (no auth required):
  GET https://{slug}.teamtailor.com/jobs.json
  Returns a JSON object with a list of job postings.
"""
import time
from typing import Optional
from urllib.parse import urljoin

import structlog

from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)


class TeamTailorConnector(BaseConnector):
    ats_type = "teamtailor"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        start = time.monotonic()
        slug = ats_identifier.strip().rstrip("/")
        domain = "teamtailor.com"
        url = f"https://{slug}.teamtailor.com/jobs.json"

        try:
            data, ms = await self.fetch(url, company_id, domain)
            jobs = self._parse_response(data, slug)
            elapsed = int((time.monotonic() - start) * 1000)
            logger.info("teamtailor_fetched", slug=slug, count=len(jobs))
            return ConnectorResult(
                company_id=company_id,
                ats_type=self.ats_type,
                jobs=jobs,
                success=True,
                response_time_ms=elapsed,
            )
        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.warning("teamtailor_failed", slug=slug, error=str(e))
            return ConnectorResult(
                company_id=company_id,
                ats_type=self.ats_type,
                success=False,
                error=str(e),
                response_time_ms=elapsed,
            )

    def _parse_response(self, data: dict | list, slug: str) -> list[RawJob]:
        """Parse TeamTailor JSON response into RawJob list."""
        jobs = []
        base_url = f"https://{slug}.teamtailor.com"

        # TeamTailor /jobs.json can return:
        #   {"jobs": [...]}  OR  a list directly
        if isinstance(data, dict):
            raw_list = data.get("jobs", data.get("data", []))
        elif isinstance(data, list):
            raw_list = data
        else:
            return []

        for raw in raw_list:
            job = self._parse_job(raw, slug, base_url)
            if job:
                jobs.append(job)

        return jobs

    def _parse_job(self, raw: dict, slug: str, base_url: str) -> Optional[RawJob]:
        # TeamTailor can return flat dicts OR JSON:API style {"id": ..., "attributes": {...}}
        if "attributes" in raw:
            attrs = raw["attributes"]
            ext_id = str(raw.get("id", ""))
        else:
            attrs = raw
            ext_id = str(raw.get("id", raw.get("external_id", "")))

        title = (attrs.get("title") or attrs.get("name") or "").strip()
        if not title:
            return None

        # Job URL
        job_url = attrs.get("url") or attrs.get("apply_url") or ""
        if not job_url:
            if ext_id:
                job_url = f"{base_url}/jobs/{ext_id}"
            else:
                job_url = f"{base_url}/jobs"

        # Location
        location = ""
        loc = attrs.get("location") or attrs.get("locations", {})
        if isinstance(loc, dict):
            location = loc.get("name", "")
        elif isinstance(loc, str):
            location = loc
        elif isinstance(loc, list) and loc:
            location = loc[0].get("name", "") if isinstance(loc[0], dict) else str(loc[0])

        # Remote
        remote = bool(attrs.get("remote_status") == "remote" or attrs.get("remote", False))
        if remote and not location:
            location = "Remote"

        # Employment type
        emp_type = attrs.get("employment_type", attrs.get("employment-type", ""))
        if isinstance(emp_type, dict):
            emp_type = emp_type.get("name", "")

        # Description
        description = attrs.get("body", attrs.get("description", ""))

        if not ext_id:
            import hashlib
            ext_id = hashlib.sha256(f"{title}|{job_url}".encode()).hexdigest()[:16]

        return RawJob(
            external_id=ext_id,
            title=title,
            company_name=slug,
            location=location,
            job_url=job_url,
            apply_url=job_url,
            description=description,
            employment_type=emp_type.upper() if emp_type else "",
            remote=remote,
            source="teamtailor",
            raw_json=raw,
        )

    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        """Required by BaseConnector — not used (overridden above)."""
        return None
