"""Lever ATS connector."""
from datetime import datetime, timezone
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

LEVER_URL = "https://api.lever.co/v0/postings/{company}?mode=json&limit=250"


class LeverConnector(BaseConnector):
    ats_type = "lever"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        domain = "api.lever.co"
        url = LEVER_URL.format(company=ats_identifier)
        try:
            data, ms = await self.fetch(url, company_id, domain)
            raw_jobs = data if isinstance(data, list) else data.get("data", [])
            jobs = []
            for raw in raw_jobs:
                try:
                    job = self._parse_job(raw, ats_identifier)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning("lever_parse_error", error=str(e))

            logger.info("lever_fetched", company=ats_identifier, count=len(jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=jobs, success=True, response_time_ms=ms
            )
        except Exception as e:
            logger.error("lever_fetch_failed", company=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        if not raw.get("id") or not raw.get("text"):
            return None

        location = ""
        categories = raw.get("categories", {})
        if isinstance(categories, dict):
            location = categories.get("location", "") or categories.get("allLocations", [""])[0] if categories.get("allLocations") else ""

        commitment = categories.get("commitment", "") if isinstance(categories, dict) else ""

        posted_at = None
        if raw.get("createdAt"):
            try:
                posted_at = datetime.fromtimestamp(raw["createdAt"] / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        desc_parts = []
        for section in raw.get("descriptionBody", {}).get("content", []) if isinstance(raw.get("descriptionBody"), dict) else []:
            if section.get("text"):
                desc_parts.append(section["text"])
        description = raw.get("descriptionPlain", "") or " ".join(desc_parts)

        return RawJob(
            external_id=raw["id"],
            title=raw["text"],
            company_name=company_name,
            location=location,
            job_url=raw.get("hostedUrl", ""),
            apply_url=raw.get("applyUrl", ""),
            description=description,
            employment_type=commitment,
            posted_at=posted_at,
            remote="remote" in location.lower() or "remote" in raw.get("text", "").lower(),
            source="lever",
            raw_json=raw,
        )
