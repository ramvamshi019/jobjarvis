"""Recruitee ATS connector — public offers API."""
from datetime import datetime, timezone
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

RECRUITEE_URL = "https://{slug}.recruitee.com/api/offers/?scope=published&limit=100&offset={offset}"


class RecruiteeConnector(BaseConnector):
    ats_type = "recruitee"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        """ats_identifier = Recruitee subdomain slug."""
        domain   = f"{ats_identifier}.recruitee.com"
        all_jobs: list[RawJob] = []
        offset   = 0

        try:
            while True:
                url  = RECRUITEE_URL.format(slug=ats_identifier, offset=offset)
                data, ms = await self.fetch(url, company_id, domain)

                offers = data.get("offers", []) if isinstance(data, dict) else []
                if not offers:
                    break

                for raw in offers:
                    try:
                        job = self._parse_job(raw, ats_identifier)
                        if job:
                            all_jobs.append(job)
                    except Exception as e:
                        logger.warning("recruitee_parse_error", error=str(e))

                # Recruitee paginates with offset
                if len(offers) < 100:
                    break
                offset += 100

            logger.info("recruitee_fetched", company=ats_identifier, count=len(all_jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=all_jobs, success=True
            )
        except Exception as e:
            logger.error("recruitee_fetch_failed", company=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _parse_job(self, raw: dict, company_slug: str) -> Optional[RawJob]:
        job_id = str(raw.get("id", ""))
        title  = raw.get("title", "")
        if not job_id or not title:
            return None

        city    = raw.get("city", "") or ""
        country = raw.get("country_code", raw.get("country", "")) or ""
        loc_str = ", ".join(p for p in [city, country] if p)
        remote  = bool(raw.get("remote", False)) or "remote" in loc_str.lower()

        posted_at = None
        for field in ["published_at", "created_at"]:
            if raw.get(field):
                try:
                    posted_at = datetime.fromisoformat(
                        str(raw[field]).replace("Z", "+00:00")
                    )
                    break
                except Exception:
                    pass

        slug_path = raw.get("slug", job_id)
        job_url   = (
            raw.get("careers_url")
            or f"https://{company_slug}.recruitee.com/o/{slug_path}"
        )

        # Map employment kind
        kind_map = {
            "full_time": "full-time", "part_time": "part-time",
            "contract": "contract", "internship": "internship",
            "freelance": "contract", "temporary": "temporary",
        }
        emp_type = kind_map.get(raw.get("kind", ""), raw.get("kind", ""))

        return RawJob(
            external_id=job_id,
            title=title,
            company_name=company_slug,
            location=loc_str,
            job_url=job_url,
            apply_url=job_url,
            description=raw.get("description", "") or "",
            employment_type=emp_type,
            posted_at=posted_at,
            remote=remote,
            source="recruitee",
            raw_json=raw,
        )
