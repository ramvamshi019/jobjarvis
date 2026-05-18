"""Workday ATS connector — paginates through all jobs via the JSON API."""
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin

import structlog

from app.connectors.base import BaseConnector, ConnectorResult, RawJob

logger = structlog.get_logger(__name__)

PAGE_SIZE = 20          # Workday's max per request
MAX_PAGES = 50          # cap at 1000 jobs per company
CONCURRENCY = 3         # parallel page fetches per company


class WorkdayConnector(BaseConnector):
    """
    Workday connector — full pagination.

    ats_identifier format: "tenant|board|shard"
      e.g. "nvidia|NVIDIA|wd5"
      e.g. "netapp|netapp|wd1"

    API endpoint:
      POST https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
      body: {"limit": 20, "offset": N, "searchText": ""}
    """
    ats_type = "workday"

    WORKDAY_TEMPLATE = (
        "https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    )

    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        start = time.monotonic()
        domain = "myworkdayjobs.com"
        try:
            parts = ats_identifier.split("|")
            tenant = parts[0]
            board  = parts[1] if len(parts) > 1 else "External"
            shard  = parts[2] if len(parts) > 2 else "wd1"
            url = self.WORKDAY_TEMPLATE.format(tenant=tenant, board=board, shard=shard)

            # ── Page 0: discover total ────────────────────────────────────────
            page0_payload = {"limit": PAGE_SIZE, "offset": 0, "searchText": ""}
            data, ms0 = await self.fetch(
                url, company_id, domain, method="POST", json_body=page0_payload
            )

            raw_jobs = data.get("jobPostings", [])
            total = data.get("total", len(raw_jobs))

            # ── Paginate remaining pages in parallel ──────────────────────────
            # Shallow first-pass: skip pagination entirely (page 0 only) so a
            # never-/rarely-scanned company costs 1 request instead of up to
            # 50. It still gets ingested + marked scanned; deep pagination
            # comes on its next (full) scan once it's proven active.
            if self.shallow:
                remaining_offsets = []
            else:
                remaining_offsets = list(range(PAGE_SIZE, min(total, PAGE_SIZE * MAX_PAGES), PAGE_SIZE))

            if remaining_offsets:
                sem = asyncio.Semaphore(CONCURRENCY)

                async def _fetch_page(offset: int) -> list[dict]:
                    async with sem:
                        try:
                            payload = {"limit": PAGE_SIZE, "offset": offset, "searchText": ""}
                            d, _ = await self.fetch(
                                url, company_id, domain, method="POST", json_body=payload
                            )
                            return d.get("jobPostings", [])
                        except Exception as exc:
                            logger.debug(
                                "workday_page_failed",
                                tenant=tenant,
                                offset=offset,
                                error=str(exc),
                            )
                            return []

                pages = await asyncio.gather(*[_fetch_page(o) for o in remaining_offsets])
                for page in pages:
                    raw_jobs.extend(page)

            jobs = [
                j
                for raw in raw_jobs
                if (j := self._parse_job(raw, ats_identifier)) is not None
            ]

            elapsed = int((time.monotonic() - start) * 1000)
            logger.info(
                "workday_fetched",
                tenant=tenant,
                board=board,
                shard=shard,
                total_api=total,
                jobs_parsed=len(jobs),
            )
            return ConnectorResult(
                company_id=company_id,
                ats_type=self.ats_type,
                jobs=jobs,
                success=True,
                response_time_ms=elapsed,
            )

        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.warning("workday_fetch_failed", ats_id=ats_identifier, error=str(e))
            return ConnectorResult(
                company_id=company_id,
                ats_type=self.ats_type,
                success=False,
                error=str(e),
                response_time_ms=elapsed,
            )

    @staticmethod
    def _parse_posted_at(raw: dict) -> Optional[datetime]:
        """
        Derive an approximate posting date.

        Workday's cxs list endpoint only exposes a human-readable ``postedOn``
        string ("Posted Today", "Posted Yesterday", "Posted 5 Days Ago",
        "Posted 30+ Days Ago"). Some tenants also include an ISO date in
        ``startDate``/``postedOnDate``. We prefer the ISO field when present
        and otherwise approximate from the relative phrase so freshness
        reflects the real age instead of our scrape time.
        """
        for key in ("startDate", "postedOnDate", "postedDate"):
            val = raw.get(key)
            if isinstance(val, str) and val:
                try:
                    return datetime.fromisoformat(
                        val.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except ValueError:
                    pass

        phrase = (raw.get("postedOn") or "").strip().lower()
        if not phrase:
            return None

        now = datetime.now(timezone.utc)
        if "today" in phrase:
            return now
        if "yesterday" in phrase:
            return now - timedelta(days=1)
        m = re.search(r"(\d+)\s*\+?\s*day", phrase)
        if m:
            return now - timedelta(days=int(m.group(1)))
        m = re.search(r"(\d+)\s*\+?\s*month", phrase)
        if m:
            return now - timedelta(days=int(m.group(1)) * 30)
        return None

    def _parse_job(self, raw: dict, ats_identifier: str) -> Optional[RawJob]:
        title = raw.get("title", raw.get("jobPostingTitle", ""))
        if not title:
            return None

        ext_path = raw.get("externalPath", "")
        ext_id = ext_path or raw.get("bulletFields", [None])[0] or title

        # Build a full apply URL from the externalPath
        parts = ats_identifier.split("|")
        tenant = parts[0]
        board  = parts[1] if len(parts) > 1 else "External"
        shard  = parts[2] if len(parts) > 2 else "wd1"
        base = f"https://{tenant}.{shard}.myworkdayjobs.com/en-US/{board}"
        job_url = urljoin(base + "/", ext_path.lstrip("/")) if ext_path else base

        # Location: Workday sometimes returns a list in bulletFields
        location = raw.get("locationsText", "")
        if not location:
            bullets = raw.get("bulletFields", [])
            if isinstance(bullets, list) and len(bullets) > 1:
                location = bullets[1]

        return RawJob(
            external_id=str(ext_id),
            title=title,
            company_name=ats_identifier.split("|")[0],  # tenant name as placeholder
            location=location,
            job_url=job_url,
            apply_url=job_url,
            posted_at=self._parse_posted_at(raw),
            source="workday",
            raw_json=raw,
        )
