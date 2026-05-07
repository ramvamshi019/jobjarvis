"""Base async ATS connector with retry, backoff, jitter, rate limiting."""
import asyncio
import hashlib
import json
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
import structlog
import httpx
from tenacity import (
    AsyncRetrying, RetryError,
    stop_after_attempt, wait_exponential, retry_if_exception_type,
    before_sleep_log
)
from app.config import settings
from app.services.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)

_rate_limiter = RateLimiter()


@dataclass
class RawJob:
    """Normalized raw job before Silver processing."""
    external_id: str
    title: str
    company_name: str
    location: str = ""
    job_url: str = ""
    apply_url: str = ""
    description: str = ""
    description_html: str = ""
    employment_type: str = ""
    posted_at: Optional[datetime] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: str = "USD"
    raw_json: dict = field(default_factory=dict)
    source: str = ""
    remote: bool = False


@dataclass
class ConnectorResult:
    company_id: int
    ats_type: str
    jobs: list[RawJob] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    response_time_ms: int = 0
    raw_response_path: Optional[str] = None


class BaseConnector(ABC):
    """Abstract base for all ATS connectors."""

    ats_type: str = "unknown"
    DEFAULT_TIMEOUT = settings.SCAN_DEFAULT_TIMEOUT_SECONDS
    MAX_RETRIES = settings.SCAN_MAX_RETRIES
    BASE_DELAY = settings.SCAN_BASE_RETRY_DELAY
    MAX_DELAY = settings.SCAN_MAX_RETRY_DELAY

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.DEFAULT_TIMEOUT),
            headers=self._default_headers(),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    def _default_headers(self) -> dict:
        return {
            "User-Agent": "JobJarvis/1.0 Career Intelligence Platform (contact@jobjarvis.ai)",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def fetch(
        self,
        url: str,
        company_id: int,
        domain: str,
        method: str = "GET",
        params: dict = None,
        json_body: dict = None,
    ) -> tuple[dict | list, int]:
        """Fetch with retry, backoff, jitter, and rate limiting."""
        await _rate_limiter.acquire(domain)
        start = time.monotonic()

        retryable = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.MAX_RETRIES),
                wait=wait_exponential(
                    multiplier=self.BASE_DELAY,
                    max=self.MAX_DELAY,
                ),
                retry=retry_if_exception_type(retryable),
                reraise=True,
            ):
                with attempt:
                    jitter = random.uniform(0, settings.SCAN_JITTER_MAX)
                    await asyncio.sleep(jitter)
                    resp = await self._client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                    )
                    resp.raise_for_status()
                    elapsed = int((time.monotonic() - start) * 1000)
                    logger.debug("fetch_ok", url=url, status=resp.status_code, ms=elapsed)
                    return resp.json(), elapsed

        except httpx.HTTPStatusError as e:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.warning("fetch_http_error", url=url, status=e.response.status_code, ms=elapsed)
            raise
        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.error("fetch_error", url=url, error=str(e), ms=elapsed)
            raise

    def _raw_hash(self, data: Any) -> str:
        raw = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:64]

    @abstractmethod
    async def fetch_jobs(self, company_id: int, ats_identifier: str) -> ConnectorResult:
        """Fetch all open jobs for a company."""
        ...

    @abstractmethod
    def _parse_job(self, raw: dict, company_name: str) -> Optional[RawJob]:
        """Parse a single raw job dict into RawJob."""
        ...
