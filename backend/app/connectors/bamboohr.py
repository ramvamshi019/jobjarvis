"""BambooHR ATS connector — parses public embed endpoint."""
import json
import re
from datetime import datetime, timezone
from typing import Optional
import structlog
from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)


class BambooHRConnector(BaseConnector):
    ats_type = "bamboohr"

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        """ats_identifier = BambooHR subdomain slug."""
        domain = f"{ats_identifier}.bamboohr.com"
        url    = f"https://{ats_identifier}.bamboohr.com/jobs/embed2.php"

        try:
            # BambooHR embed returns HTML — we extract the JSON payload
            resp = await self._client.get(url, timeout=15)
            resp.raise_for_status()
            html = resp.text

            jobs_data = self._extract_jobs_json(html)
            if jobs_data is None:
                # Try alternate JSON endpoint
                alt_url  = f"https://{ats_identifier}.bamboohr.com/careers/list"
                resp2    = await self._client.get(alt_url, timeout=10)
                if resp2.status_code == 200:
                    try:
                        jobs_data = resp2.json()
                        if isinstance(jobs_data, dict):
                            jobs_data = jobs_data.get("result", jobs_data.get("jobs", []))
                    except Exception:
                        jobs_data = []
                else:
                    jobs_data = []

            jobs: list[RawJob] = []
            for raw in (jobs_data or []):
                try:
                    job = self._parse_job(raw, ats_identifier)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning("bamboohr_parse_error", error=str(e))

            logger.info("bamboohr_fetched", company=ats_identifier, count=len(jobs))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                jobs=jobs, success=True
            )
        except Exception as e:
            logger.error("bamboohr_fetch_failed", company=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id, ats_type=self.ats_type,
                success=False, error=str(e)
            )

    def _extract_jobs_json(self, html: str) -> Optional[list]:
        """Extract the jobs array embedded in BambooHR's HTML page."""
        # Pattern 1: JSON.parse('{"jobs":[...]}')
        m = re.search(r"JSON\.parse\('(\{.*?\"jobs\".*?\})'\)", html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1).replace("\\'", "'"))
                return data.get("jobs", [])
            except Exception:
                pass

        # Pattern 2: BambooHR.options = {...}
        m = re.search(r"BambooHR\.options\s*=\s*(\{.*?\});", html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return data.get("jobs", [])
            except Exception:
                pass

        # Pattern 3: window.__INITIAL_STATE__ or similar
        m = re.search(r'"jobs"\s*:\s*(\[.*?\])', html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass

        return None

    def _parse_job(self, raw: dict, company_slug: str) -> Optional[RawJob]:
        job_id = str(raw.get("id", raw.get("jobId", "")))
        title  = raw.get("jobTitle", raw.get("title", raw.get("name", "")))
        if not job_id or not title:
            return None

        location = raw.get("location", {})
        if isinstance(location, dict):
            city    = location.get("city", "")
            state   = location.get("state", "")
            country = location.get("country", "US")
            loc_str = ", ".join(p for p in [city, state, country] if p)
        else:
            loc_str = str(location) if location else ""

        remote = bool(raw.get("isRemote", False)) or "remote" in loc_str.lower()

        posted_at = None
        for field in ["datePosted", "created_at", "postedDate"]:
            if raw.get(field):
                try:
                    posted_at = datetime.fromisoformat(
                        str(raw[field]).replace("Z", "+00:00")
                    )
                    break
                except Exception:
                    pass

        job_url = (
            raw.get("jobOpeningUrl")
            or raw.get("url")
            or f"https://{company_slug}.bamboohr.com/jobs/view.php?id={job_id}"
        )

        desc = raw.get("description", raw.get("jobDescription", "")) or ""

        return RawJob(
            external_id=job_id,
            title=title,
            company_name=company_slug,
            location=loc_str,
            job_url=job_url,
            apply_url=raw.get("applyUrl", job_url),
            description=desc,
            employment_type=raw.get("employmentType", raw.get("jobType", "")),
            posted_at=posted_at,
            remote=remote,
            source="bamboohr",
            raw_json=raw,
        )
