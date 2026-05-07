"""
ATS dispatcher — routes fetch requests to the correct connector.

Supported providers: greenhouse | lever | ashby | smartrecruiters | workday
All other providers log a warning and return [].

fetch_jobs_with_fallback() probes slug variations and alternate providers
automatically so the pipeline never hard-fails on a stale ATS config.
"""
import asyncio
import json
import logging
import os
import re
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Early freshness cutoff ─────────────────────────────────────────────────────
# Same env var as the pipeline so both are always in sync.
_FRESHNESS_HOURS: int = int(os.getenv("JOB_FRESHNESS_HOURS", "24"))

# ── Provider failure cache (file-backed, count-based) ─────────────────────────
# key: str(company_id) + "|" + provider_name   (stringified for JSON compat)
# value: {"count": int, "last_fail": float}
# A provider is blocked when count >= _FAIL_THRESHOLD within CACHE_TTL seconds.
_CACHE_FILE: str = os.getenv("ATS_CACHE_FILE", "/tmp/ats_cache.json")
CACHE_TTL: float = 21600              # 6 hours in seconds
_FAIL_THRESHOLD: int = 2              # block after 2 confirmed 404s


def _cache_key(company_id: Optional[int], provider: str) -> str:
    """Stable string key for JSON serialisation: '<company_id>|<provider>'."""
    return f"{company_id}|{provider.lower()}"


def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(_CACHE_FILE, "w") as fh:
            json.dump(cache, fh)
    except Exception as exc:
        logger.warning("[CACHE_SAVE_ERROR] %s", exc)


# Load persisted cache at import time.
FAILED_PROVIDER_CACHE: dict[str, dict] = _load_cache()

# ── Per-run metrics ────────────────────────────────────────────────────────────
# Incremented inside connectors; reset by the pipeline at the start of each run.
_ATS_METRICS: dict[str, int] = {
    "jobs_skipped_early_old":      0,
    "providers_skipped_by_cache":  0,
    "providers_blocked_by_threshold": 0,
    "jobs_dropped_by_fetch_limit": 0,
}


def reset_ats_metrics() -> None:
    """Zero out per-run counters.  Call once at the start of each pipeline run."""
    _ATS_METRICS["jobs_skipped_early_old"]         = 0
    _ATS_METRICS["providers_skipped_by_cache"]     = 0
    _ATS_METRICS["providers_blocked_by_threshold"] = 0
    _ATS_METRICS["jobs_dropped_by_fetch_limit"]    = 0


def get_ats_metrics() -> dict[str, int]:
    """Return a snapshot of the current per-run counters."""
    return dict(_ATS_METRICS)


# ── Provider failure cache helpers ────────────────────────────────────────────

def _cache_add(key: str) -> None:
    """Increment 404 failure count; persist to disk immediately."""
    entry = FAILED_PROVIDER_CACHE.get(key)
    now = _time.time()
    if not entry:
        FAILED_PROVIDER_CACHE[key] = {"count": 1, "last_fail": now}
    else:
        entry["count"] += 1
        entry["last_fail"] = now
    _save_cache(FAILED_PROVIDER_CACHE)
    logger.debug(
        "[CACHE_ADD] key=%s count=%d",
        key, FAILED_PROVIDER_CACHE[key]["count"],
    )


def _cache_blocked(key: str) -> bool:
    """
    Return True when the provider has >= _FAIL_THRESHOLD 404s within CACHE_TTL.
    Evicts expired entries automatically and persists the eviction.
    """
    entry = FAILED_PROVIDER_CACHE.get(key)
    count = entry["count"] if entry else 0
    if not entry:
        logger.debug("[CACHE_CHECK] key=%s count=%d blocked=%s", key, 0, False)
        return False
    now = _time.time()
    if now - entry["last_fail"] > CACHE_TTL:
        del FAILED_PROVIDER_CACHE[key]
        _save_cache(FAILED_PROVIDER_CACHE)
        logger.debug("[CACHE_CHECK] key=%s count=%d blocked=%s (expired)", key, count, False)
        return False
    blocked = entry["count"] >= _FAIL_THRESHOLD
    logger.debug("[CACHE_CHECK] key=%s count=%d blocked=%s", key, count, blocked)
    return blocked


# ── Freshness helpers ──────────────────────────────────────────────────────────

def parse_posted_date(posted_str: str | None) -> Optional[datetime]:
    """Parse a posted_at string → UTC-aware datetime, or None if absent/unparseable."""
    if not posted_str:
        return None
    dt = datetime.fromisoformat(str(posted_str).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_fresh(posted_str: str | None, now: datetime) -> bool:
    """
    Hard cutoff: return False only when the age is confidently > _FRESHNESS_HOURS.
    Unknown / unparseable dates are kept (True) — unknown age ≠ stale.
    No fuzzy tolerance, no confidence logic.
    """
    try:
        posted_dt = parse_posted_date(posted_str)
    except Exception:
        return True   # unparseable → keep
    if not posted_dt:
        return True   # no date → keep
    age_hours = (now - posted_dt).total_seconds() / 3600
    return age_hours <= _FRESHNESS_HOURS


# ── Low-level HTTP helper ──────────────────────────────────────────────────────

async def _fetch_with_retry(
    client: httpx.AsyncClient, url: str, max_retries: int = 3
) -> Any:
    """GET JSON from *url* with exponential backoff. Raises on final failure."""
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_err = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
    raise last_err  # type: ignore[misc]


# ── Greenhouse ─────────────────────────────────────────────────────────────────

async def fetch_greenhouse_jobs(
    client: httpx.AsyncClient, company_slug: str
) -> List[Dict[str, Any]]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs"
    try:
        data = await _fetch_with_retry(client, url)
        now  = datetime.now(timezone.utc)
        jobs = []
        for job in data.get("jobs", []):
            posted_str = job.get("updated_at")
            if not _is_fresh(posted_str, now):
                _ATS_METRICS["jobs_skipped_early_old"] += 1
                logger.debug("[EARLY_OLD_SKIP] title=%s", job.get("title", ""))
                continue
            jobs.append({
                "title":       job.get("title", ""),
                "location":    job.get("location", {}).get("name", ""),
                "job_url":     job.get("absolute_url", ""),
                "external_id": str(job.get("id", "")),
                "source":      "greenhouse",
                "posted_at":   posted_str,
            })
            if len(jobs) >= 300:
                _ATS_METRICS["jobs_dropped_by_fetch_limit"] += 1
                logger.debug("[FETCH_LIMIT_HIT] provider=greenhouse count=%d", len(jobs))
                break
        return jobs
    except Exception as exc:
        logger.error("greenhouse_fetch_error slug=%s error=%s", company_slug, exc)
        raise


async def fetch_greenhouse_job_detail(
    client: httpx.AsyncClient, company_slug: str, job_id: str
) -> str:
    """Fetch full job description from Greenhouse detail API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs/{job_id}"
    try:
        data = await _fetch_with_retry(client, url)
        # Prefer plain text if available, fallback to HTML
        return data.get("content") or data.get("description", "")
    except Exception as exc:
        logger.warning("greenhouse_detail_error slug=%s id=%s error=%s", company_slug, job_id, exc)
        return ""


# ── Lever ──────────────────────────────────────────────────────────────────────

async def fetch_lever_jobs(
    client: httpx.AsyncClient, company_slug: str
) -> List[Dict[str, Any]]:
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    try:
        data = await _fetch_with_retry(client, url)
        now  = datetime.now(timezone.utc)
        jobs = []
        for job in (data if isinstance(data, list) else []):
            created_ts = job.get("createdAt", 0)
            posted_at = (
                datetime.fromtimestamp(created_ts / 1000).isoformat()
                if created_ts else None
            )
            if not _is_fresh(posted_at, now):
                _ATS_METRICS["jobs_skipped_early_old"] += 1
                logger.debug("[EARLY_OLD_SKIP] title=%s", job.get("text", ""))
                continue
            jobs.append({
                "title":       job.get("text", ""),
                "location":    job.get("categories", {}).get("location", ""),
                "job_url":     job.get("hostedUrl", ""),
                "external_id": str(job.get("id", "")),
                "source":      "lever",
                "posted_at":   posted_at,
                "description": job.get("descriptionPlain", ""),
            })
            if len(jobs) >= 300:
                _ATS_METRICS["jobs_dropped_by_fetch_limit"] += 1
                logger.debug("[FETCH_LIMIT_HIT] provider=lever count=%d", len(jobs))
                break
        return jobs
    except Exception as exc:
        logger.error("lever_fetch_error slug=%s error=%s", company_slug, exc)
        raise


async def fetch_lever_job_detail(
    client: httpx.AsyncClient, company_slug: str, job_id: str
) -> str:
    """Lever usually returns description in list view, but detail is available."""
    url = f"https://api.lever.co/v0/postings/{company_slug}/{job_id}"
    try:
        data = await _fetch_with_retry(client, url)
        return data.get("descriptionPlain") or data.get("description", "")
    except Exception as exc:
        logger.warning("lever_detail_error slug=%s id=%s error=%s", company_slug, job_id, exc)
        return ""


# ── Ashby ──────────────────────────────────────────────────────────────────────

async def fetch_ashby_jobs(
    client: httpx.AsyncClient, company_slug: str
) -> List[Dict[str, Any]]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
    try:
        data = await _fetch_with_retry(client, url)
        now  = datetime.now(timezone.utc)
        jobs = []
        for job in data.get("jobPostings", []):
            posted_str = job.get("publishedDate")
            if not _is_fresh(posted_str, now):
                _ATS_METRICS["jobs_skipped_early_old"] += 1
                logger.debug("[EARLY_OLD_SKIP] title=%s", job.get("title", ""))
                continue
            loc = job.get("location") or job.get("locationName") or ""
            jobs.append({
                "title":       job.get("title", ""),
                "location":    loc,
                "job_url":     job.get("jobPostingUrl", ""),
                "external_id": str(job.get("id", "")),
                "source":      "ashby",
                "posted_at":   posted_str,
                "description": job.get("descriptionHtml", ""),
            })
            if len(jobs) >= 300:
                _ATS_METRICS["jobs_dropped_by_fetch_limit"] += 1
                logger.debug("[FETCH_LIMIT_HIT] provider=ashby count=%d", len(jobs))
                break
        return jobs
    except Exception as exc:
        logger.error("ashby_fetch_error slug=%s error=%s", company_slug, exc)
        raise


# ── SmartRecruiters ────────────────────────────────────────────────────────────

async def fetch_smartrecruiters_jobs(
    client: httpx.AsyncClient, company_slug: str
) -> List[Dict[str, Any]]:
    """Paginated SmartRecruiters public jobs endpoint (max 100/page)."""
    base_url = (
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        "?status=PUBLIC&limit=100&offset={offset}"
    )
    jobs: List[Dict[str, Any]] = []
    offset = 0
    now    = datetime.now(timezone.utc)
    try:
        while True:
            url  = base_url.format(slug=company_slug, offset=offset)
            data = await _fetch_with_retry(client, url)
            page = data.get("content", [])
            if not page:
                break
            for job in page:
                posted_str = job.get("releasedDate")
                if not _is_fresh(posted_str, now):
                    _ATS_METRICS["jobs_skipped_early_old"] += 1
                    logger.debug("[EARLY_OLD_SKIP] title=%s", job.get("name", ""))
                    continue
                loc_obj = job.get("location") or {}
                loc = ", ".join(filter(None, [
                    loc_obj.get("city"),
                    loc_obj.get("region"),
                    loc_obj.get("country"),
                ]))
                jobs.append({
                    "title":       job.get("name", ""),
                    "location":    loc,
                    "job_url":     job.get("ref", ""),
                    "external_id": str(job.get("id", "")),
                    "source":      "smartrecruiters",
                    "posted_at":   posted_str,
                })
                if len(jobs) >= 300:
                    _ATS_METRICS["jobs_dropped_by_fetch_limit"] += 1
                    logger.debug("[FETCH_LIMIT_HIT] provider=smartrecruiters count=%d", len(jobs))
                    return jobs
            offset += len(page)
            if len(page) < 100:
                break
        return jobs
    except Exception as exc:
        logger.error("smartrecruiters_fetch_error slug=%s error=%s", company_slug, exc)
        raise

# ── Workday ───────────────────────────────────────────────────────────────────
# Workday uses a company-specific subdomain and a POST-based JSON API.
# ats_identifier format: "tenant"              → board defaults to "External"
#                        "tenant|BoardName"    → explicit board
# The subdomain pattern: {tenant}.wd1.myworkdayjobs.com
# (some tenants use wd2, wd3 — we try wd1 first then wd5 as fallback)

_WORKDAY_URL = (
    "https://{tenant}.wd{n}.myworkdayjobs.com"
    "/wday/cxs/{tenant}/{board}/jobs"
)
_WORKDAY_VARIANTS = [1, 5, 3, 12]   # most-common instance numbers


async def fetch_workday_jobs(
    client: httpx.AsyncClient, ats_identifier: str
) -> List[Dict[str, Any]]:
    """
    Paginated Workday CXS JSON API.
    Returns normalised job dicts or raises on total failure.
    """
    parts  = ats_identifier.split("|", 1)
    tenant = parts[0].strip()
    board  = parts[1].strip() if len(parts) > 1 else "External"

    # Find a working instance number (wd1, wd5, …)
    base_url: str | None = None
    for n in _WORKDAY_VARIANTS:
        probe_url = _WORKDAY_URL.format(tenant=tenant, board=board, n=n)
        try:
            # Workday returns 200 even for empty boards on valid tenants;
            # a 404/403 means this instance number doesn't match.
            r = await client.post(
                probe_url,
                json={"limit": 1, "offset": 0, "searchText": ""},
                timeout=10.0,
            )
            if r.status_code == 200:
                base_url = _WORKDAY_URL.format(tenant=tenant, board=board, n=n)
                break
        except Exception:
            continue

    if not base_url:
        logger.warning("workday_no_valid_instance tenant=%s", tenant)
        return []

    jobs: List[Dict[str, Any]] = []
    offset     = 0
    page_size  = 20   # Workday default page size
    _MAX_PAGES = 3    # fetch at most 3 pages (60 jobs) per company
    page_num   = 0

    while True:
        try:
            resp = await client.post(
                base_url,
                json={"limit": page_size, "offset": offset, "searchText": ""},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("workday_page_error tenant=%s offset=%d error=%s", tenant, offset, exc)
            break

        postings = data.get("jobPostings", [])
        if not postings:
            break

        seen_urls: set[str] = set()  # intra-batch dedup
        now_wd = datetime.now(timezone.utc)

        for raw in postings:
            title    = raw.get("title") or raw.get("jobPostingTitle", "")
            location = raw.get("locationsText", "")
            ext_path = raw.get("externalPath", "")
            ext_id   = ext_path.lstrip("/") or title
            job_url  = (
                f"https://{tenant}.wd1.myworkdayjobs.com{ext_path}"
                if ext_path else ""
            )

            # Quality filters
            if not title:    continue
            if not location: continue
            if job_url and job_url in seen_urls: continue
            if job_url:
                seen_urls.add(job_url)

            # Early freshness filter — uses shared _FRESHNESS_HOURS
            posted_raw = raw.get("postedOn") or raw.get("startDate")
            if not _is_fresh(posted_raw, now_wd):
                _ATS_METRICS["jobs_skipped_early_old"] += 1
                logger.debug("[EARLY_OLD_SKIP] title=%s", title)
                continue

            jobs.append({
                "title":       title,
                "location":    location,
                "job_url":     job_url,
                "external_id": ext_id,
                "source":      "workday",
                "posted_at":   posted_raw,
                "description": "",
            })

        offset   += len(postings)
        page_num += 1
        if len(postings) < page_size:
            break
        if page_num >= _MAX_PAGES:
            _ATS_METRICS["jobs_dropped_by_fetch_limit"] += 1
            logger.debug("[FETCH_LIMIT_HIT] provider=workday count=%d", len(jobs))
            break
        if offset >= 1000:   # absolute safety cap
            break

    logger.info("workday_fetched tenant=%s count=%d", tenant, len(jobs))
    return jobs


async def fetch_jobs_from_ats(
    client: httpx.AsyncClient,
    ats_provider: str,
    company_slug: str,
) -> List[Dict[str, Any]]:
    """
    Route to the correct ATS connector.
    Raises on fetch error so the caller's circuit breaker can count failures.
    """
    p = ats_provider.lower()
    if p == "greenhouse":
        return await fetch_greenhouse_jobs(client, company_slug)
    elif p == "lever":
        return await fetch_lever_jobs(client, company_slug)
    elif p == "ashby":
        return await fetch_ashby_jobs(client, company_slug)
    elif p == "smartrecruiters":
        return await fetch_smartrecruiters_jobs(client, company_slug)
    elif p == "workday":
        return await fetch_workday_jobs(client, company_slug)
    else:
        logger.warning("ats_dispatcher_unknown_provider provider=%s slug=%s", ats_provider, company_slug)
        return []
# ── Dispatcher ──────────────────────────────────────────────────────────────────

async def fetch_job_details(
    client: httpx.AsyncClient,
    ats_type: str,
    company_slug: str,
    job_id: str
) -> str:
    """Dispatcher to fetch full job description based on ATS type."""
    ats_type = ats_type.lower()
    if ats_type == "greenhouse":
        return await fetch_greenhouse_job_detail(client, company_slug, job_id)
    elif ats_type == "lever":
        return await fetch_lever_job_detail(client, company_slug, job_id)
    # Add others as needed. Ashby/Workday usually include description in list view.
    return ""


# ── ATS auto-detection / fallback infrastructure ───────────────────────────────

# Providers to probe when the primary ATS/slug fails.  Ordered by prevalence.
_ATS_FALLBACK_ORDER: list[str] = ["greenhouse", "lever", "ashby"]


def _slug_variations(slug: str, domain: Optional[str] = None) -> list[str]:
    """
    Return an ordered list of candidate slugs to try.

    Strategy:
      1. Primary slug (always first — highest confidence)
      2. TLD-stripped domain base  ("stripe.com" → "stripe")
      3. Bare domain               ("stripe.com")

    Duplicates and empty strings are removed while preserving order.
    Capped at 4 candidates to limit HTTP traffic.
    """
    seen: list[str] = []

    def _add(s: str) -> None:
        s = (s or "").strip().lower()
        if s and s not in seen:
            seen.append(s)

    _add(slug)

    if domain:
        # Strip common TLDs → bare name: "stripe.com" → "stripe"
        base = re.sub(
            r"\.(com|io|ai|co|net|org|app|tech|careers|jobs|us|uk|de|ca)$",
            "",
            domain.lower(),
        )
        base = re.sub(r"^(www|careers|jobs)\.", "", base)
        _add(base)
        # Dots → hyphens for multi-part domains: "my.company.io" → "my-company"
        _add(base.replace(".", "-"))
        # Full domain in case the ATS uses it directly
        _add(domain.lower())

    return seen[:5]  # safety cap


async def fetch_jobs_with_fallback(
    client: httpx.AsyncClient,
    primary_ats: str,
    primary_slug: str,
    company_domain: Optional[str] = None,
    skip_providers: Optional[set] = None,
    company_id: Optional[int] = None,
) -> tuple[list[dict], str, str, bool]:
    """
    Fetch jobs from the best available ATS / slug combination.

    Probe order:
      1. Primary provider + all slug variations  (skipped if primary is in
         skip_providers, e.g. when its circuit breaker is open)
      2. Fallback providers (Greenhouse → Lever → Ashby, excluding primary)
         with primary slug only

    Provider cache (Fix 2):
      Any provider that returned 404 for this company_id is added to
      FAILED_PROVIDER_CACHE and skipped for _CACHE_TTL_SECONDS (6 h).

    Returns:
        (jobs, used_ats, used_slug, fallback_used)
        where fallback_used=True means we deviated from the stored config.

    Raises:
        The last exception if every attempt fails.
    """
    primary_lower = primary_ats.lower()
    # Normalise primary slug: strip ".com", lowercase, strip whitespace
    primary_slug  = re.sub(r"\.com$", "", primary_slug.strip().lower())
    slugs = _slug_variations(primary_slug, company_domain)
    _skip = {s.lower() for s in (skip_providers or set())}

    fallback_providers = [p for p in _ATS_FALLBACK_ORDER if p != primary_lower]

    last_exc: Exception | None = None

    # ── Phase 1: Primary provider + slug variations ───────────────────────────
    primary_key = _cache_key(company_id, primary_lower)
    if _cache_blocked(primary_key):
        # Always evaluated — independent of circuit-breaker skip set.
        _ATS_METRICS["providers_skipped_by_cache"]     += 1
        _ATS_METRICS["providers_blocked_by_threshold"] += 1
        logger.info("[CACHE_BLOCKED] key=%s provider=%s", primary_key, primary_lower)
    elif primary_lower not in _skip:
        for slug in slugs:
            # Normalise each candidate slug as well
            slug = re.sub(r"\.com$", "", slug.strip().lower())
            try:
                jobs = await fetch_jobs_from_ats(client, primary_lower, slug)
                if jobs is not None and len(jobs) > 0:
                    fallback_used = (slug != primary_slug)
                    logger.info(
                        "[ATS_SUCCESS] provider=%s slug=%s fallback=%s jobs=%d",
                        primary_lower, slug, fallback_used, len(jobs),
                    )
                    return jobs, primary_lower, slug, fallback_used
                # Empty result — treat as failure for this provider
                _cache_add(primary_key)
                logger.info(
                    "[CACHE_ADD_EMPTY] key=%s provider=%s slug=%s",
                    primary_key, primary_lower, slug,
                )
                continue
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code == 404:
                    _cache_add(primary_key)
                    logger.debug(
                        "[ATS_PROBE] 404 provider=%s slug=%s — trying next",
                        primary_lower, slug,
                    )
                else:
                    logger.warning(
                        "[ATS_PROBE] http_%d provider=%s slug=%s — trying next",
                        exc.response.status_code, primary_lower, slug,
                    )
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                logger.warning(
                    "[ATS_PROBE] network_error provider=%s slug=%s error=%s — skipping provider",
                    primary_lower, slug, exc,
                )
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "[ATS_PROBE] error provider=%s slug=%s error=%s",
                    primary_lower, slug, exc,
                )

    # ── Phase 2: Fallback providers with primary slug ─────────────────────────
    for provider in fallback_providers:
        key = _cache_key(company_id, provider)
        if _cache_blocked(key):
            _ATS_METRICS["providers_skipped_by_cache"]     += 1
            _ATS_METRICS["providers_blocked_by_threshold"] += 1
            logger.info("[CACHE_BLOCKED] key=%s provider=%s", key, provider)
            continue
        try:
            jobs = await fetch_jobs_from_ats(client, provider, primary_slug)
            if jobs is not None and len(jobs) > 0:
                logger.info(
                    "[ATS_FALLBACK_USED] fallback_provider=%s slug=%s jobs=%d "
                    "(original_provider=%s original_slug=%s)",
                    provider, primary_slug, len(jobs), primary_lower, primary_slug,
                )
                return jobs, provider, primary_slug, True
            # Empty result — treat as failure for this fallback provider
            _cache_add(key)
            logger.info(
                "[CACHE_ADD_EMPTY] key=%s provider=%s slug=%s",
                key, provider, primary_slug,
            )
            continue
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code == 404:
                _cache_add(key)
                logger.debug(
                    "[ATS_PROBE] 404 provider=%s slug=%s — trying next",
                    provider, primary_slug,
                )
            else:
                logger.warning(
                    "[ATS_PROBE] http_%d provider=%s slug=%s — trying next",
                    exc.response.status_code, provider, primary_slug,
                )
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            last_exc = exc
            logger.warning(
                "[ATS_PROBE] network_error provider=%s slug=%s error=%s — skipping",
                provider, primary_slug, exc,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[ATS_PROBE] error provider=%s slug=%s error=%s",
                provider, primary_slug, exc,
            )

    raise last_exc or RuntimeError(
        f"All ATS fetch attempts failed for slug={primary_slug!r}"
    )
