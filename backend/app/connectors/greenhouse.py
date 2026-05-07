"""Greenhouse ATS connector — API-first, no Playwright."""
from datetime import datetime
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

GH_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
GH_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"


class GreenhouseConnector(BaseConnector):
    ats_type = "greenhouse"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        domain = "greenhouse.io"
        url = GH_BOARD_URL.format(token=ats_identifier)
        try:
            data, ms = await self.fetch(url, company_id, domain)
            raw_jobs = data.get("jobs", [])
            jobs = []
            for raw in raw_jobs:
                try:
                    job = self._parse_job(raw, ats_identifier)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning("gh_parse_error", job_id=raw.get("id"), error=str(e))

            logger.info("greenhouse_fetched", token=ats_identifier, count=len(jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=jobs, success=True, response_time_ms=ms
            )
        except Exception as e:
            logger.error("greenhouse_fetch_failed", token=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        if not raw.get("id") or not raw.get("title"):
            return None

        # Extract location
        offices = raw.get("offices", [])
        location_parts = [o.get("name", "") for o in offices if o.get("name")]
        location = ", ".join(location_parts) if location_parts else raw.get("location", {}).get("name", "")

        # Extract remote
        remote = any(
            "remote" in (o.get("name", "")).lower() for o in offices
        ) or "remote" in raw.get("title", "").lower()

        # Posted date
        posted_at = None
        if raw.get("updated_at"):
            try:
                posted_at = datetime.fromisoformat(raw["updated_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Description
        desc = ""
        if raw.get("content"):
            desc = raw["content"]

        return RawJob(
            external_id=str(raw["id"]),
            title=raw["title"],
            company_name=company_name,
            location=location,
            job_url=raw.get("absolute_url", ""),
            apply_url=raw.get("absolute_url", ""),
            description=desc,
            description_html=desc,
            posted_at=posted_at,
            remote=remote,
            source="greenhouse",
            raw_json=raw,
        )
