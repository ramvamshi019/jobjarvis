"""
AI-assisted auto-healer for failing companies.

Runs daily. When a company has consecutive_failures >= 3 it tries to:
  1. Re-probe all ATS platforms with the current slug
  2. Derive alternative slugs from the company name and probe those
  3. If something works → update the record and reset failure count
  4. If nothing works after 10+ consecutive failures → deactivate the company

For Workday companies: tries common board name patterns automatically.
"""
import asyncio
import re
from datetime import datetime, timezone, timedelta

import httpx
import structlog
from sqlalchemy import select, and_

from app.workers.celery_app import celery_app
from app.database import AsyncSessionLocal, async_engine
from app.models.company import Company

logger = structlog.get_logger(__name__)

HEAL_THRESHOLD = 3       # consecutive failures before healing attempt
DISABLE_THRESHOLD = 10   # consecutive failures before disabling
MAX_COMPANIES_PER_RUN = 100

# Common Workday board name patterns to try when the current one fails
WORKDAY_BOARD_VARIANTS = [
    "{tenant}",
    "{tenant}ExternalCareerSite",
    "{Tenant}ExternalCareerSite",
    "External",
    "Careers",
    "Jobs",
    "{tenant}Jobs",
    "{Tenant}",
    "ExternalCareerSite",
    "CareerSite",
]

WORKDAY_SHARDS = ["wd1", "wd3", "wd5", "wd12", "wd2"]


def _run_async(coro):
    async def _wrapper():
        await async_engine.dispose()
        return await coro
    return asyncio.run(_wrapper())


@celery_app.task(name="app.workers.healer_tasks.heal_failing_companies",
                 soft_time_limit=1800, max_retries=1)
def heal_failing_companies():
    """Daily AI-assisted task to fix companies that are consistently failing."""
    return _run_async(_heal_async())


@celery_app.task(name="app.workers.healer_tasks.heal_single_company",
                 soft_time_limit=120, max_retries=1)
def heal_single_company(company_id: int):
    """Heal a specific company (called after repeated scan failures)."""
    return _run_async(_heal_one_async(company_id))


# ── ATS probe helpers ─────────────────────────────────────────────────────────

async def _probe_greenhouse(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=7)
        return r.status_code == 200 and len(r.json().get("jobs", [])) > 0
    except Exception:
        return False


async def _probe_lever(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1", timeout=7)
        data = r.json()
        return r.status_code == 200 and isinstance(data, list) and len(data) > 0
    except Exception:
        return False


async def _probe_ashby(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=7)
        return r.status_code == 200 and len(r.json().get("jobPostings", [])) > 0
    except Exception:
        return False


async def _probe_smartrecruiters(client: httpx.AsyncClient, slug: str) -> bool:
    try:
        r = await client.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 1}, timeout=7,
        )
        return r.status_code == 200 and len(r.json().get("content", [])) > 0
    except Exception:
        return False


async def _probe_workday(client: httpx.AsyncClient, tenant: str, board: str, shard: str) -> bool:
    try:
        url = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
        r = await client.post(url, json={"limit": 1, "offset": 0}, timeout=10)
        return r.status_code == 200 and len(r.json().get("jobPostings", [])) > 0
    except Exception:
        return False


def _derive_slugs(name: str) -> list[str]:
    """Derive candidate slugs from company name."""
    n = name.lower()
    for suffix in [" inc", " corp", " ltd", " llc", " technologies", " systems",
                   " solutions", " software", " labs", " ai", " tech", ", inc",
                   ", corp", ", ltd", ".", ","]:
        n = n.replace(suffix, "")
    n = n.strip()
    h = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    plain = re.sub(r"[^a-z0-9]+", "", n)
    candidates = [h, plain]
    if h.endswith("-ai"):
        candidates.append(h[:-3])
    # Also try without common words
    for word in ["-technologies", "-systems", "-solutions", "-software"]:
        if h.endswith(word):
            candidates.append(h[: -len(word)])
    return list(dict.fromkeys(s for s in candidates if len(s) >= 2))


async def _try_heal_workday(client: httpx.AsyncClient, company: Company) -> tuple[str, str] | None:
    """Try all Workday board+shard combinations for a failing Workday company."""
    parts = (company.ats_identifier or "").split("|")
    tenant = parts[0] if parts else company.name.lower().replace(" ", "")
    tenant = re.sub(r"[^a-z0-9]", "", tenant)

    for shard in WORKDAY_SHARDS:
        for pattern in WORKDAY_BOARD_VARIANTS:
            board = pattern.replace("{tenant}", tenant).replace("{Tenant}", tenant.capitalize())
            if await _probe_workday(client, tenant, board, shard):
                new_id = f"{tenant}|{board}|{shard}"
                logger.info("healer_workday_fixed", company=company.name,
                            old=company.ats_identifier, new=new_id)
                return "workday", new_id
    return None, None


async def _try_heal_generic(client: httpx.AsyncClient, company: Company) -> tuple[str, str] | None:
    """Try Greenhouse, Lever, Ashby, SmartRecruiters with slug variants."""
    slugs = _derive_slugs(company.name)
    # Always include the current identifier as a candidate
    if company.ats_identifier and company.ats_identifier not in slugs:
        slugs.insert(0, company.ats_identifier)

    for slug in slugs:
        if await _probe_greenhouse(client, slug):
            logger.info("healer_fixed", company=company.name, new_ats="greenhouse", slug=slug)
            return "greenhouse", slug
        if await _probe_lever(client, slug):
            logger.info("healer_fixed", company=company.name, new_ats="lever", slug=slug)
            return "lever", slug
        if await _probe_ashby(client, slug):
            logger.info("healer_fixed", company=company.name, new_ats="ashby", slug=slug)
            return "ashby", slug
        if await _probe_smartrecruiters(client, slug):
            logger.info("healer_fixed", company=company.name, new_ats="smartrecruiters", slug=slug)
            return "smartrecruiters", slug

    return None, None


# ── Main heal logic ───────────────────────────────────────────────────────────

async def _heal_one_async(company_id: int) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Company).where(Company.id == company_id))
        company = result.scalar_one_or_none()
        if not company:
            return {"error": "company_not_found"}
        return await _heal_company(db, company)


async def _heal_async() -> dict:
    """Find all failing companies and attempt to fix them."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Company).where(
                and_(
                    Company.active == True,
                    Company.consecutive_failures >= HEAL_THRESHOLD,
                )
            ).order_by(Company.consecutive_failures.desc()).limit(MAX_COMPANIES_PER_RUN)
        )
        companies = list(result.scalars().all())

    logger.info("healer_start", candidates=len(companies))

    fixed = 0
    disabled = 0
    unchanged = 0

    for company in companies:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Company).where(Company.id == company.id))
            c = result.scalar_one_or_none()
            if not c:
                continue
            outcome = await _heal_company(db, c)
            if outcome.get("action") == "fixed":
                fixed += 1
            elif outcome.get("action") == "disabled":
                disabled += 1
            else:
                unchanged += 1

    logger.info("healer_done", fixed=fixed, disabled=disabled, unchanged=unchanged)
    return {"fixed": fixed, "disabled": disabled, "unchanged": unchanged}


async def _heal_company(db, company: Company) -> dict:
    """Attempt to heal a single company. Commits result to DB."""

    # Hard disable: too many consecutive failures
    if company.consecutive_failures >= DISABLE_THRESHOLD:
        company.active = False
        company.notes = (
            f"Auto-disabled after {company.consecutive_failures} consecutive failures "
            f"on {datetime.now(timezone.utc).date().isoformat()}"
        )
        await db.commit()
        logger.info("healer_disabled", company=company.name,
                    failures=company.consecutive_failures)
        return {"action": "disabled", "company": company.name}

    # Try to find working ATS
    async with httpx.AsyncClient(
        headers={"User-Agent": "JobJarvis/1.0 healer"},
        follow_redirects=True,
    ) as client:
        if company.ats_type == "workday":
            new_ats, new_id = await _try_heal_workday(client, company)
        else:
            new_ats, new_id = await _try_heal_generic(client, company)

        # If generic didn't find anything and it's not workday, also try workday
        if new_ats is None and company.ats_type != "workday":
            new_ats, new_id = await _try_heal_workday(client, company)

    if new_ats and new_id:
        old_ats = company.ats_type
        old_id = company.ats_identifier
        company.ats_type = new_ats
        company.ats_identifier = new_id
        company.consecutive_failures = 0
        company.failure_count = 0
        company.next_scan_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        company.notes = (
            f"Auto-healed {datetime.now(timezone.utc).date().isoformat()}: "
            f"{old_ats}/{old_id} → {new_ats}/{new_id}"
        )
        await db.commit()
        return {"action": "fixed", "company": company.name,
                "old": f"{old_ats}/{old_id}", "new": f"{new_ats}/{new_id}"}

    # Nothing found — schedule next retry with longer backoff
    company.next_scan_at = datetime.now(timezone.utc) + timedelta(hours=24)
    await db.commit()
    logger.info("healer_no_fix", company=company.name, failures=company.consecutive_failures)
    return {"action": "unchanged", "company": company.name}
