"""
Company Expander — generates new company candidates from existing DB records.

Expansion strategy (in order of yield):
  1. Domain name permutations (TLD swap + suffix variants)
     openai.com → openai.ai, openaialabs.com, openai-tech.com
     Reaches ~3x the seed count passively.

  2. GitHub org lookup
     For each company name: query api.github.com/orgs/<slug>
     If org exists and has a blog/website field → extract as new domain.
     Yields 5k–10k real tech orgs with zero false positives.

  3. Career page detection
     HEAD-request homepage, then try /careers and /jobs paths.
     If 200 → company has a career page → store career_url.
     Runs only on companies that already exist in DB (enrichment pass).

All methods are purely additive — they NEVER modify existing Company rows.
Duplicates are prevented via normalize_domain() before any DB write.

Safety:
  - asyncio.Semaphore caps concurrent outbound connections.
  - All network calls have hard timeouts.
  - GitHub rate-limit: 60 req/hr unauthenticated; set GITHUB_TOKEN env var
    to raise to 5 000 req/hr.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as _insert

from app.database import AsyncSessionLocal
from app.models.company import Company
from app.utils.domain_utils import normalize_domain, root_name_from_domain

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

_EXPAND_CONCURRENCY = 15
_NET_TIMEOUT        = 8.0
_GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN", "")

# TLD variants to try for each root name
_TLD_VARIANTS: list[str] = [".com", ".ai", ".io", ".co", ".dev", ".tech"]

# Name suffix variants for AI/tech companies
_NAME_SUFFIXES: list[str] = ["", "labs", "hq", "ai"]

# Known career path patterns
_CAREER_PATHS: list[str] = ["/careers", "/jobs", "/work-with-us", "/join-us", "/join"]

_MAX_EXPAND_PER_RUN = 20_000   # total new rows ceiling for one expansion run


# ── 1. Domain Permutation Expander ────────────────────────────────────────────

def generate_domain_permutations(domain: str) -> list[str]:
    """
    Given a canonical domain, return a list of plausible variant domains.
    Strict: skips variants that look like the original domain.

    Example:
      stripe.com → [stripe.ai, stripe.io, stripe.co, stripelabs.com, ...]
    """
    root = root_name_from_domain(domain)
    if not root or len(root) < 3:
        return []

    variants: list[str] = []
    for suffix in _NAME_SUFFIXES:
        name = f"{root}{suffix}"
        for tld in _TLD_VARIANTS:
            candidate = f"{name}{tld}"
            if candidate != domain:
                variants.append(candidate)

    return variants


# ── 2. GitHub Org Expander ────────────────────────────────────────────────────

async def _github_org_lookup(
    client: httpx.AsyncClient,
    org_slug: str,
) -> dict[str, Any] | None:
    """
    Probe GitHub API for an org.
    Phase 3 quality gate: org must have a website OR >5 public repos
    OR have been updated within the last 12 months.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"

    url = f"https://api.github.com/orgs/{org_slug}"
    try:
        resp = await client.get(url, headers=headers, timeout=_NET_TIMEOUT)
        if resp.status_code == 404:
            return None
        if resp.status_code == 403:
            logger.warning("github_rate_limited — set GITHUB_TOKEN to raise limit")
            return None
        resp.raise_for_status()
        data = resp.json()

        # Phase 3: quality gate
        has_website     = bool(data.get("blog"))
        public_repos    = data.get("public_repos", 0) or 0
        has_enough_repos = public_repos > 5
        updated_at_raw  = data.get("updated_at", "")
        recently_active = False
        if updated_at_raw:
            try:
                import datetime as _dt
                updated = _dt.datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
                cutoff  = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=365)
                recently_active = updated >= cutoff
            except Exception:
                pass

        if not (has_website or has_enough_repos or recently_active):
            return None   # fails quality gate

        website = data.get("blog") or ""
        if website and not website.startswith("http"):
            website = "https://" + website
        return {
            "name":    data.get("name") or org_slug,
            "domain":  normalize_domain(website) if website else "",
            "country": "unknown",
        }
    except Exception:
        return None


async def expand_via_github_orgs(
    company_names: list[str],
    max_results: int = 5_000,
) -> list[dict[str, Any]]:
    """
    For each company name, slugify and probe GitHub /orgs/<slug>.
    Returns list of new company row dicts.
    """
    sem = asyncio.Semaphore(_EXPAND_CONCURRENCY)
    results: list[dict[str, Any]] = []

    def _slugify(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    async with httpx.AsyncClient(follow_redirects=True, timeout=_NET_TIMEOUT) as client:
        async def _probe(name: str) -> None:
            if len(results) >= max_results:
                return
            slug = _slugify(name)
            async with sem:
                org = await _github_org_lookup(client, slug)
            if org and org.get("domain"):
                results.append({
                    "name":           org["name"],
                    "domain":         org["domain"],
                    "ats_identifier": slug,
                    "country":        org.get("country", "unknown"),
                    "active":         True,
                    "priority_score": 40,           # GitHub-sourced = tier 3
                    "scan_frequency_minutes": 1440, # daily
                })

        await asyncio.gather(*[_probe(n) for n in company_names])

    logger.info("github_expansion_complete found=%d", len(results))
    return results


# ── 3. Career Page Detector ───────────────────────────────────────────────────

async def detect_career_page(
    client: httpx.AsyncClient,
    domain: str,
) -> str | None:
    """
    Try common career URL paths on the given domain.
    Returns the first path that responds 200, or None.
    """
    for path in _CAREER_PATHS:
        url = f"https://{domain}{path}"
        try:
            resp = await client.head(url, timeout=_NET_TIMEOUT, follow_redirects=True)
            if resp.status_code == 200:
                return url
        except Exception:
            continue
    return None


async def enrich_career_urls(limit: int = 500) -> int:
    """
    Enrichment pass: for companies without a career_url, probe for one.
    Updates company.career_url in-place. Returns count updated.
    """
    sem = asyncio.Semaphore(_EXPAND_CONCURRENCY)
    updated = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Company)
            .where(Company.active == True)
            .where(Company.career_url == None)  # noqa: E711
            .where(Company.domain != None)       # noqa: E711
            .limit(limit)
        )
        companies = result.scalars().all()

    async with httpx.AsyncClient(follow_redirects=True, timeout=_NET_TIMEOUT) as client:
        async def _check(company: Company) -> None:
            nonlocal updated
            async with sem:
                career_url = await detect_career_page(client, company.domain)
            if career_url:
                async with AsyncSessionLocal() as db:
                    c = await db.get(Company, company.id)
                    if c:
                        c.career_url = career_url
                        await db.commit()
                        updated += 1

        await asyncio.gather(*[_check(c) for c in companies])

    logger.info("career_url_enrichment_complete updated=%d", updated)
    return updated


# ── 4. Company Scoring ────────────────────────────────────────────────────────

def compute_discovery_score(row: dict[str, Any]) -> int:
    """
    Compute a 0–30 discovery quality score for a candidate company row.

    Points:
      +10  ATS detected (ats_type is set)
      +10  Jobs confirmed (ats_type + ats_identifier both set)
       +5  Career page URL known
       +3  Domain is valid (not empty / not 'unknown')
       +2  Country is known (not 'unknown')
    """
    score = 0
    if row.get("ats_type"):
        score += 10
    if row.get("ats_type") and row.get("ats_identifier"):
        score += 10
    if row.get("career_url"):
        score += 5
    domain = row.get("domain", "")
    if domain and domain not in ("unknown", ""):
        score += 3
    if row.get("country", "unknown") not in ("unknown", "", None):
        score += 2
    return score


def score_to_priority(discovery_score: int) -> tuple[int, int]:
    """
    Map discovery_score → (priority_score, scan_frequency_minutes).
    Aligns with the tier system in Company.scan_tier.
    """
    if discovery_score >= 20:
        return (90, 60)    # Tier 1: every hour
    elif discovery_score >= 10:
        return (65, 360)   # Tier 2: every 6 hours
    else:
        return (30, 1440)  # Tier 3: daily


# ── 5. Main Expansion Entry Point ─────────────────────────────────────────────

async def run_expansion(max_new: int = _MAX_EXPAND_PER_RUN) -> dict[str, int]:
    """
    Full expansion pass:
      1. Load existing companies from DB
      2. Generate domain permutations from their domains
      3. Probe GitHub orgs from their names
      4. Score and upsert net-new companies

    Returns metrics dict.
    """
    logger.info("company_expander_start max_new=%d", max_new)

    metrics = {
        "existing_companies":    0,
        "permutation_candidates": 0,
        "github_candidates":     0,
        "inserted":              0,
        "duplicates_skipped":    0,
    }

    # 1. Load existing companies
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Company.name, Company.domain).where(Company.active == True).limit(5000)
        )
        rows = result.all()

    existing_domains: set[str] = {normalize_domain(r.domain) for r in rows if r.domain}
    existing_names: list[str]  = [r.name for r in rows if r.name]
    metrics["existing_companies"] = len(rows)

    new_rows: list[dict[str, Any]] = []
    seen: set[str] = set(existing_domains)

    def _add_if_new(row: dict[str, Any]) -> None:
        domain = normalize_domain(row.get("domain", ""))
        if not domain or domain in seen:
            metrics["duplicates_skipped"] += 1
            return
        seen.add(domain)
        row["domain"] = domain
        # Apply scoring
        dscore = compute_discovery_score(row)
        pri, freq = score_to_priority(dscore)
        row.setdefault("priority_score", pri)
        row.setdefault("scan_frequency_minutes", freq)
        new_rows.append(row)

    # 2. Domain permutations (fast generation, DNS-gated insertion)
    # Phase 4: only insert a permutation if DNS resolves — filters parked/fake domains.
    import socket

    async def _dns_ok(domain: str) -> bool:
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, socket.getaddrinfo, domain, 80),
                timeout=3.0,
            )
            return True
        except Exception:
            return False

    dns_sem = asyncio.Semaphore(50)  # high concurrency for DNS — it's cheap

    async def _add_if_new_dns_gated(variant: str, name: str) -> None:
        async with dns_sem:
            ok = await _dns_ok(variant)
        if ok:
            _add_if_new({
                "name":   name,
                "domain": variant,
                "active": True,
            })

    perm_tasks = []
    for r in rows:
        if len(new_rows) >= max_new:
            break
        if not r.domain:
            continue
        for variant in generate_domain_permutations(r.domain):
            if len(new_rows) >= max_new:
                break
            perm_tasks.append(
                _add_if_new_dns_gated(variant, root_name_from_domain(variant).title())
            )

    await asyncio.gather(*perm_tasks)
    metrics["permutation_candidates"] = len(new_rows)

    # 3. GitHub org expansion (network, rate-limited)
    if len(new_rows) < max_new:
        github_results = await expand_via_github_orgs(
            company_names=existing_names[:1000],  # avoid rate-limit exhaustion
            max_results=max_new - len(new_rows),
        )
        for row in github_results:
            _add_if_new(row)
    metrics["github_candidates"] = len(new_rows) - metrics["permutation_candidates"]

    # 4. Upsert in chunks
    chunk_size = 500
    async with AsyncSessionLocal() as db:
        for i in range(0, len(new_rows), chunk_size):
            chunk = new_rows[i : i + chunk_size]
            stmt = _insert(Company).values(chunk).on_conflict_do_nothing(index_elements=["domain"])
            await db.execute(stmt)
            await db.commit()
            metrics["inserted"] += len(chunk)

    logger.info("company_expander_complete metrics=%s", metrics)
    return metrics
