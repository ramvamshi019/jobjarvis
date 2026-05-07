"""
ATS Detection Service.

Given a company domain/name, probes known ATS endpoints to determine
which provider hosts their job board and what the correct slug is.

Supports: Greenhouse, Lever, Ashby, SmartRecruiters, Workday, iCIMS
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0
_CONCURRENCY = 10


@dataclass
class ATSResult:
    provider: str
    slug: str
    job_count: int
    confidence: float   # 0.0–1.0


# ── Provider probe definitions ─────────────────────────────────────────────────

_ATS_PROBES: list[tuple[str, str, str]] = [
    # (provider, url_template, jobs_key)
    # jobs_key="" means root is a list
    ("greenhouse",      "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs"),
    ("lever",           "https://api.lever.co/v0/postings/{slug}?mode=json",       ""),
    ("ashby",           "https://api.ashbyhq.com/posting-api/job-board/{slug}",    "jobPostings"),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=10", "content"),
]

_WORKDAY_WD_VARIANTS = [1, 5, 3]


def _slug_candidates(name: str, domain: Optional[str]) -> list[str]:
    """Generate slug candidates from company name + domain."""
    candidates: list[str] = []

    def _add(s: str) -> None:
        s = re.sub(r"[^a-z0-9\-]", "", (s or "").strip().lower().replace(" ", "-"))
        if s and s not in candidates:
            candidates.append(s)

    _add(name)
    _add(name.replace(" ", ""))

    if domain:
        base = re.sub(
            r"\.(com|io|ai|co|net|org|app|tech|us|uk|de|ca)$", "",
            domain.lower()
        )
        base = re.sub(r"^(www|careers|jobs)\.", "", base)
        _add(base)
        _add(base.replace(".", "-"))

    return candidates[:5]


async def _probe_ats(
    client: httpx.AsyncClient,
    provider: str,
    url_tpl: str,
    jobs_key: str,
    slug: str,
) -> Optional[ATSResult]:
    url = url_tpl.format(slug=slug)
    try:
        resp = await client.get(url, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if jobs_key:
            jobs = data.get(jobs_key, [])
        else:
            jobs = data if isinstance(data, list) else []
        count = len(jobs)
        # A valid board returns 200 even with 0 jobs — but empty + random slug likely wrong
        confidence = 0.95 if count > 0 else 0.6
        return ATSResult(provider=provider, slug=slug, job_count=count, confidence=confidence)
    except Exception:
        return None


async def _probe_workday(
    client: httpx.AsyncClient, tenant: str
) -> Optional[ATSResult]:
    for n in _WORKDAY_WD_VARIANTS:
        url = f"https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/External/jobs"
        try:
            resp = await client.post(
                url,
                json={"limit": 5, "offset": 0, "searchText": ""},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                count = len(data.get("jobPostings", []))
                return ATSResult(
                    provider="workday",
                    slug=tenant,
                    job_count=count,
                    confidence=0.95,
                )
        except Exception:
            continue
    return None


async def detect_ats(
    name: str,
    domain: Optional[str] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Optional[ATSResult]:
    """
    Probe all supported ATS providers and return the best match.
    Returns None if no ATS is detected.
    """
    sem = semaphore or asyncio.Semaphore(_CONCURRENCY)
    slugs = _slug_candidates(name, domain)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = []
        for provider, url_tpl, jobs_key in _ATS_PROBES:
            for slug in slugs:
                tasks.append(_probe_ats(client, provider, url_tpl, jobs_key, slug))

        # Also probe Workday
        for slug in slugs:
            tasks.append(_probe_workday(client, slug))

        async with sem:
            results = await asyncio.gather(*tasks, return_exceptions=True)

    valid = [r for r in results if isinstance(r, ATSResult)]
    if not valid:
        return None

    # Prefer higher confidence, then higher job count
    best = max(valid, key=lambda r: (r.confidence, r.job_count))
    logger.info(
        "[ATS_DETECT] company=%s provider=%s slug=%s jobs=%d conf=%.2f",
        name, best.provider, best.slug, best.job_count, best.confidence,
    )
    return best


async def detect_ats_batch(
    companies: list[tuple[str, Optional[str]]],  # (name, domain)
    concurrency: int = _CONCURRENCY,
) -> list[tuple[str, Optional[ATSResult]]]:
    """Detect ATS for a batch of companies concurrently."""
    sem = asyncio.Semaphore(concurrency)
    results = []

    async def _detect_one(name: str, domain: Optional[str]) -> tuple[str, Optional[ATSResult]]:
        result = await detect_ats(name, domain, semaphore=sem)
        return name, result

    tasks = [_detect_one(name, domain) for name, domain in companies]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if not isinstance(r, Exception)]
