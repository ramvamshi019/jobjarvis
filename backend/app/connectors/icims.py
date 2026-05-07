"""iCIMS ATS connector skeleton."""
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)


class ICIMSConnector(BaseConnector):
    """iCIMS skeleton. Full API requires OAuth + customer ID."""
    ats_type = "icims"

    ICIMS_TEMPLATE = "https://api.icims.com/customers/{customer_id}/jobs?status=A"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        """ats_identifier = iCIMS customer ID"""
        domain = "api.icims.com"
        try:
            url = self.ICIMS_TEMPLATE.format(customer_id=ats_identifier)
            data, ms = await self.fetch(url, company_id, domain)
            raw_jobs = data.get("items", []) if isinstance(data, dict) else []
            jobs = [j for raw in raw_jobs if (j := self._parse_job(raw, ats_identifier)) is not None]
            logger.info("icims_fetched", customer=ats_identifier, count=len(jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=jobs, success=True, response_time_ms=ms
            )
        except Exception as e:
            logger.warning("icims_fetch_failed", customer=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=f"iCIMS skeleton: {e}"
            )

    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        title = raw.get("jobtitle", raw.get("title", ""))
        ext_id = str(raw.get("id", ""))
        if not title:
            return None
        return RawJob(
            external_id=ext_id,
            title=title,
            company_name=company_name,
            location=raw.get("joblocation", {}).get("value", "") if isinstance(raw.get("joblocation"), dict) else "",
            job_url=raw.get("url", ""),
            source="icims",
            raw_json=raw,
        )
