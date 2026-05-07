"""SmartRecruiters ATS connector."""
from datetime import datetime, timezone
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

SR_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings?status=PUBLIC&limit=100&offset={offset}"


class SmartRecruitersConnector(BaseConnector):
    ats_type = "smartrecruiters"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        domain = "api.smartrecruiters.com"
        all_jobs = []
        offset = 0
        page_size = 100

        try:
            while True:
                url = SR_URL.format(company=ats_identifier, offset=offset)
                data, ms = await self.fetch(url, company_id, domain)

                content = data.get("content", [])
                if not content:
                    break

                for raw in content:
                    try:
                        job = self._parse_job(raw, ats_identifier)
                        if job:
                            all_jobs.append(job)
                    except Exception as e:
                        logger.warning("sr_parse_error", error=str(e))

                total = data.get("totalFound", 0)
                offset += page_size
                if offset >= total:
                    break

            logger.info("smartrecruiters_fetched", company=ats_identifier, count=len(all_jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=all_jobs, success=True
            )
        except Exception as e:
            logger.error("sr_fetch_failed", company=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        if not raw.get("id") or not raw.get("name"):
            return None

        location_obj = raw.get("location", {})
        city = location_obj.get("city", "")
        country = location_obj.get("country", "")
        remote = location_obj.get("remote", False)
        location = f"{city}, {country}".strip(", ") if city or country else ""

        posted_at = None
        if raw.get("releasedDate"):
            try:
                posted_at = datetime.fromisoformat(raw["releasedDate"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        job_url = f"https://jobs.smartrecruiters.com/{company_name}/{raw['id']}"

        return RawJob(
            external_id=raw["id"],
            title=raw["name"],
            company_name=raw.get("company", {}).get("name", company_name),
            location=location,
            job_url=job_url,
            apply_url=job_url,
            description=raw.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", ""),
            employment_type=raw.get("typeOfEmployment", {}).get("label", ""),
            posted_at=posted_at,
            remote=remote or "remote" in location.lower(),
            source="smartrecruiters",
            raw_json=raw,
        )
