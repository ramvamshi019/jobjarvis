"""Workable ATS connector — public job board API."""
from datetime import datetime, timezone
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

WORKABLE_LIST_URL  = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"
WORKABLE_JOB_URL   = "https://apply.workable.com/api/v3/accounts/{slug}/jobs/{shortcode}"


class WorkableConnector(BaseConnector):
    ats_type = "workable"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        domain = "apply.workable.com"
        slug   = ats_identifier
        all_jobs: list[RawJob] = []
        next_cursor: Optional[str] = None

        try:
            while True:
                payload: dict = {"limit": 50, "details": True}
                if next_cursor:
                    payload["token"] = next_cursor

                data, ms = await self.fetch(
                    WORKABLE_LIST_URL.format(slug=slug),
                    company_id, domain,
                    method="POST",
                    json_body=payload,
                )

                for raw in data.get("results", []):
                    try:
                        job = self._parse_job(raw, slug)
                        if job:
                            all_jobs.append(job)
                    except Exception as e:
                        logger.warning("workable_parse_error", error=str(e))

                next_cursor = data.get("paging", {}).get("next")
                if not next_cursor:
                    break

            logger.info("workable_fetched", company=slug, count=len(all_jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=all_jobs, success=True
            )
        except Exception as e:
            logger.error("workable_fetch_failed", company=slug, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _parse_job(self, raw: dict, company_slug: str) -> Optional[RawJob]:
        if not raw.get("id") or not raw.get("title"):
            return None

        loc   = raw.get("location", {}) or {}
        city  = loc.get("city", "") or ""
        region = loc.get("region", "") or ""
        country = loc.get("country_code", "") or loc.get("country", "") or ""
        remote = bool(loc.get("telecommuting", False))

        parts = [p for p in [city, region, country] if p]
        location_str = ", ".join(parts)

        posted_at = None
        if raw.get("created_at"):
            try:
                posted_at = datetime.fromisoformat(
                    raw["created_at"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # description may be present if details=True was sent
        desc  = raw.get("description", "") or ""
        req   = raw.get("requirements", "") or ""
        bens  = raw.get("benefits", "") or ""
        full_desc = "\n\n".join(p for p in [desc, req, bens] if p)

        job_url = raw.get("url", "") or raw.get("shortlink", "") or \
                  f"https://apply.workable.com/{company_slug}/j/{raw.get('shortcode','')}"

        return RawJob(
            external_id=raw["id"],
            title=raw["title"],
            company_name=company_slug,
            location=location_str,
            job_url=job_url,
            apply_url=raw.get("application_url", job_url),
            description=full_desc,
            employment_type=raw.get("employment_type", ""),
            posted_at=posted_at,
            remote=remote or "remote" in location_str.lower(),
            source="workable",
            raw_json=raw,
        )
