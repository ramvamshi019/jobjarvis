"""Breezy HR ATS connector — clean public JSON API."""
from datetime import datetime, timezone
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)


class BreezyConnector(BaseConnector):
    ats_type = "breezy"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        """ats_identifier = Breezy subdomain slug."""
        domain = f"{ats_identifier}.breezy.hr"
        url    = f"https://{ats_identifier}.breezy.hr/json"

        try:
            data, ms = await self.fetch(url, company_id, domain)

            positions = data.get("positions", []) if isinstance(data, dict) else []
            jobs: list[RawJob] = []
            for raw in positions:
                try:
                    if raw.get("state", "published") != "published":
                        continue
                    job = self._parse_job(raw, ats_identifier)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning("breezy_parse_error", error=str(e))

            logger.info("breezy_fetched", company=ats_identifier, count=len(jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=jobs, success=True, response_time_ms=ms
            )
        except Exception as e:
            logger.error("breezy_fetch_failed", company=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _parse_job(self, raw: dict, company_slug: str) -> Optional[RawJob]:
        job_id = str(raw.get("_id", raw.get("id", "")))
        title  = raw.get("name", raw.get("title", ""))
        if not job_id or not title:
            return None

        city    = raw.get("city", "") or ""
        state   = raw.get("state", raw.get("state_name", "")) or ""
        country = raw.get("country", "") or ""
        loc_str = ", ".join(p for p in [city, state, country] if p)
        remote  = "remote" in loc_str.lower() or "remote" in title.lower()

        posted_at = None
        for field in ["published_at", "created_at", "updated_at"]:
            if raw.get(field):
                try:
                    posted_at = datetime.fromisoformat(
                        str(raw[field]).replace("Z", "+00:00")
                    )
                    break
                except Exception:
                    pass

        friendly_url = (
            raw.get("friendly_json_url", "")
            or f"https://{company_slug}.breezy.hr/p/{job_id}"
        )
        # Convert JSON url to HTML url
        job_url = friendly_url.replace("/json", "").rstrip("/")

        # Map Breezy experience levels
        exp_map = {
            "entry_level": "entry", "mid_level": "mid",
            "senior_level": "senior", "manager": "lead",
            "director": "lead", "executive": "lead",
        }
        experience = exp_map.get(raw.get("experience", ""), "")

        # Map employment type
        type_map = {
            "full_time": "full-time", "part_time": "part-time",
            "contract": "contract", "internship": "internship",
            "temporary": "temporary", "volunteer": "volunteer",
        }
        emp_type = type_map.get(raw.get("type", ""), raw.get("type", ""))

        return RawJob(
            external_id=job_id,
            title=title,
            company_name=company_slug,
            location=loc_str,
            job_url=job_url,
            apply_url=f"https://{company_slug}.breezy.hr/p/{job_id}/apply",
            description=raw.get("description", "") or "",
            employment_type=emp_type,
            posted_at=posted_at,
            remote=remote,
            source="breezy",
            raw_json=raw,
        )
