"""Ashby ATS connector."""
from datetime import datetime, timezone
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{company}"
ASHBY_JOBS_URL = "https://api.ashbyhq.com/posting-api/job-board/{company}/jobs"


class AshbyConnector(BaseConnector):
    ats_type = "ashby"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        domain = "api.ashbyhq.com"
        url = ASHBY_JOBS_URL.format(company=ats_identifier)
        try:
            data, ms = await self.fetch(url, company_id, domain)
            raw_jobs = data.get("jobs", []) if isinstance(data, dict) else data
            jobs = []
            for raw in raw_jobs:
                try:
                    job = self._parse_job(raw, ats_identifier)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning("ashby_parse_error", error=str(e))

            logger.info("ashby_fetched", company=ats_identifier, count=len(jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=jobs, success=True, response_time_ms=ms
            )
        except Exception as e:
            logger.error("ashby_fetch_failed", company=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        if not raw.get("id") or not raw.get("title"):
            return None

        location = raw.get("locationName", "") or ""
        employment = raw.get("employmentType", "")

        posted_at = None
        if raw.get("publishedAt"):
            try:
                posted_at = datetime.fromisoformat(raw["publishedAt"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        desc = raw.get("descriptionHtml", "") or raw.get("description", "")
        job_url = raw.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{company_name}/{raw['id']}"

        return RawJob(
            external_id=raw["id"],
            title=raw["title"],
            company_name=company_name,
            location=location,
            job_url=job_url,
            apply_url=job_url,
            description=desc,
            description_html=desc,
            employment_type=employment,
            posted_at=posted_at,
            remote="remote" in location.lower() or raw.get("isRemote", False),
            source="ashby",
            raw_json=raw,
        )
