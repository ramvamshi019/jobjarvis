"""
AI-driven company discovery.

Every hour, picks the next "theme" from a rotating list (industries, cities,
funding stages, technologies) and asks Claude to list ~50 US companies
matching that theme along with their websites.  Each candidate is then
probed for its careers page; if a known ATS is detected the company is
upserted into the companies table; if not, it lands as ats='unknown' and
the existing heal pipeline will probe it later.

Why this works:
  • Claude has broad knowledge of which companies exist and are hiring in
    different industries / locations / stages.
  • Themes rotate so we cover the full landscape over ~1-2 days.
  • Each call costs ~$0.015 with Haiku 4.5  →  ~$11/month at hourly.
  • The careers-page probe is the same we use elsewhere, so detected ATS
    URLs immediately enter the existing scan pipeline.

State:
  Theme rotation index is persisted in Redis at key `ai_disco:theme_idx`.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Optional
from urllib.parse import urlparse

import asyncpg
import httpx
import structlog

from app.workers.celery_app import celery_app
from app.workers.jobboard_tasks import _run_async

logger = structlog.get_logger(__name__)

sys.path.insert(0, "/app/scripts")
try:
    from discovery_lib import detect_ats, upsert_company  # type: ignore
    _DISCOVERY_AVAILABLE = True
except Exception:
    _DISCOVERY_AVAILABLE = False

_DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
_TIMEOUT = httpx.Timeout(45.0, connect=10.0)


# ════════════════════════════════════════════════════════════════════════════
#  Rotating themes — covered roughly twice per day at hourly cadence
# ════════════════════════════════════════════════════════════════════════════

THEMES: list[str] = [
    # Industry verticals — US tech employers
    "AI infrastructure companies (LLM training, MLOps, model serving, GPU clouds)",
    "AI application companies (vertical AI agents, AI copilots, AI SaaS)",
    "Cybersecurity startups (SIEM, EDR, AppSec, identity, cloud security)",
    "Fintech startups (payments, banking infrastructure, lending, crypto)",
    "Healthcare technology companies (telehealth, EMR, biotech software)",
    "Developer tools (databases, observability, APIs, deployment platforms)",
    "Climate tech and clean energy startups (carbon, batteries, grid)",
    "Robotics, drones, and autonomous systems companies",
    "Vertical SaaS (legaltech, edtech, proptech, agritech, restech)",
    "Data engineering and analytics platforms (lakehouse, ETL, BI)",
    "Cloud infrastructure and DevOps tooling startups",
    "Defense tech and dual-use government contractors",
    "Hardware companies that hire software engineers heavily",
    "Marketing technology and customer-data platforms",
    "Productivity SaaS (CRM, project mgmt, collaboration, comms)",
    "E-commerce infrastructure and retail tech companies",
    "Logistics and supply chain technology",
    "Open source companies with venture funding",
    "Web3 and crypto infrastructure (not consumer crypto)",
    "Bio-tech companies that hire data engineers and ML engineers",

    # City-targeted batches
    "Recently funded Series A-C startups based in San Francisco",
    "Recently funded Series A-C startups based in New York City",
    "Recently funded Series A-C startups based in Boston",
    "Recently funded Series A-C startups based in Seattle",
    "Recently funded Series A-C startups based in Los Angeles",
    "Recently funded Series A-C startups based in Austin",
    "Recently funded Series A-C startups based in Denver/Boulder",
    "Recently funded Series A-C startups based in Chicago",
    "Recently funded Series A-C startups based in Washington DC",
    "Recently funded Series A-C startups based in Atlanta",

    # Stage / batch focused
    "Y Combinator companies from the last 3 batches that are hiring",
    "Techstars graduate companies actively hiring",
    "Sequoia, a16z, and First Round portfolio companies hiring engineers",
    "Bessemer, Greylock, and Khosla portfolio companies hiring engineers",
    "Mid-sized public tech companies that frequently post jobs (200-2000 employees)",
    "Unicorns ($1B+ private) that are actively hiring",

    # Tech-skill focused
    "Companies hiring Rust engineers",
    "Companies hiring Go engineers",
    "Companies hiring ML / deep learning research engineers",
    "Companies hiring data platform / data infrastructure engineers",
    "Companies hiring platform / SRE / infrastructure engineers",

    # ── New-grad / entry-level focused themes ─────────────────────────────
    # These bias the hourly Claude rotation toward companies with explicit
    # University Recruiting programs and active 2026 new-grad pipelines.
    "US tech companies running 2026 new-grad SWE programs (university recruiting)",
    "US tech companies that explicitly hire MS / MEng new-grad engineers (CS, AI/ML, robotics)",
    "Companies that sponsor H-1B visas for new-grad software engineers in the US",
    "Fortune 500 companies with formal early-career rotational engineering programs",
    "US tech companies with active 2026 summer software engineering internship listings",
    "Mid-sized US tech companies (Series C-D) that hire entry-level engineers",
    "Quant trading firms that hire new-grad engineers (Jane Street, Two Sigma, Citadel, HRT, Jump, Tower Research, DE Shaw, etc.)",
    "AI/ML companies hiring new-grad research engineers and ML residents",
    "US tech companies with published L3 / L4 / SDE-I new-grad SWE leveling",
    "Bay Area, NYC, and Seattle startups posting entry-level SWE roles in the last 30 days",
    "US tech companies hiring new-grad data engineers and data scientists",
    "Defense / aerospace / dual-use tech companies hiring US-citizen new grads",
    "Cybersecurity companies with new-grad security engineering tracks",
    "Robotics and autonomous systems companies hiring new-grad MS/PhD engineers",
    "Biotech and healthcare-tech companies hiring new-grad software engineers",
]


# ════════════════════════════════════════════════════════════════════════════
#  Claude prompt + call
# ════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a research assistant tracking hiring-active US tech employers.

Given a theme, you return a JSON list of companies that:
  1. Are US-based or have a substantial US engineering presence
  2. Are currently active / not shut down
  3. Have a real careers page on their own domain or a known ATS (Greenhouse, Lever, Ashby, Workday, etc.)
  4. Would plausibly hire software engineers, data engineers, ML engineers, or related tech roles

OUTPUT FORMAT — exactly this JSON, nothing else:
{"companies": [
  {"name": "Company Name", "website": "https://company.com"},
  ...
]}

Rules:
  - Return at least 40 companies, up to 60.
  - Use the company's official primary domain (e.g. anthropic.com), not LinkedIn / Crunchbase / news links.
  - No duplicates.
  - Do not include shell / inactive / acquired-and-shutdown companies.
  - Skip megacaps everyone already knows (Google, Meta, Amazon, Apple, Microsoft).
"""

USER_PROMPT_TEMPLATE = """Theme: {theme}

Return the JSON list of companies now."""


async def ask_claude_for_companies(theme: str) -> list[dict]:
    """Single Claude call, returns a list of {name, website} dicts."""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not api_key:
        logger.warning("ai_disco_no_api_key")
        return []

    model = os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001"
    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.warning("ai_disco_anthropic_pkg_missing")
        return []

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": USER_PROMPT_TEMPLATE.format(theme=theme)}],
        )
        text = msg.content[0].text if msg.content else ""
    except Exception as e:
        logger.exception("ai_disco_claude_call_failed", err=str(e))
        return []

    # Parse JSON out of the response (Claude usually returns just JSON,
    # but be defensive in case it includes a code-fence or commentary).
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        logger.warning("ai_disco_no_json_in_response", text=text[:300])
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        logger.warning("ai_disco_bad_json", err=str(e), text=text[:300])
        return []

    companies = data.get("companies") or []
    out: list[dict] = []
    for c in companies:
        name = (c.get("name") or "").strip()
        site = (c.get("website") or "").strip()
        if not name or not site:
            continue
        if not site.startswith("http"):
            site = "https://" + site
        out.append({"name": name[:200], "website": site})
    logger.info("ai_disco_received", theme=theme[:60], count=len(out))
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Probe each company's careers page → detect ATS → upsert
# ════════════════════════════════════════════════════════════════════════════

_ATS_HOSTS = [
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "smartrecruiters.com", "myworkdayjobs.com", "icims.com",
    "teamtailor.com", "bamboohr.com", "jobvite.com", "recruitee.com",
]

_NOISE = re.compile(r"(twitter|facebook|instagram|linkedin|youtube|crunchbase)", re.I)


async def _probe_careers(client: httpx.AsyncClient, home: str) -> Optional[str]:
    """Try `/careers`, `/jobs` etc.  Return an ATS URL if found, else the
    careers page URL itself, else None."""
    try:
        parsed = urlparse(home)
        base = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None
    if not base or _NOISE.search(home):
        return None

    for path in ("/careers", "/jobs", "/about/careers", "/company/careers", "/work-with-us"):
        url = f"{base}{path}"
        try:
            r = await client.get(url, timeout=10.0, follow_redirects=True)
            if r.status_code != 200:
                continue
            text = r.text[:80_000]
            for h in _ATS_HOSTS:
                m = re.search(r"https?://[^\"'\s>]+" + re.escape(h) + r"[^\"'\s>]*", text, re.I)
                if m:
                    return m.group(0)
            return url
        except Exception:
            continue
    # Last resort — the homepage itself might have an embedded ATS link
    try:
        r = await client.get(home, timeout=10.0, follow_redirects=True)
        if r.status_code == 200:
            text = r.text[:80_000]
            for h in _ATS_HOSTS:
                m = re.search(r"https?://[^\"'\s>]+" + re.escape(h) + r"[^\"'\s>]*", text, re.I)
                if m:
                    return m.group(0)
    except Exception:
        pass
    return None


async def _upsert_ai_companies(candidates: list[dict]) -> dict:
    if not candidates or not _DISCOVERY_AVAILABLE:
        return {"discovered": len(candidates), "ats_known": 0, "inserted": 0}

    conn = await asyncpg.connect(_DB_DSN)
    ats_known = inserted = 0

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        # Probe in parallel batches of 10
        for i in range(0, len(candidates), 10):
            batch = candidates[i:i+10]
            careers = await asyncio.gather(
                *[_probe_careers(client, c["website"]) for c in batch],
                return_exceptions=True,
            )
            for c, url in zip(batch, careers):
                if not isinstance(url, str) or not url:
                    continue
                match = detect_ats(url)
                if match:
                    ats_type, slug = match
                    ats_known += 1
                else:
                    ats_type = "unknown"
                    slug = re.sub(r"[^a-z0-9]+", "-", c["name"].lower())[:60].strip("-")
                if not slug:
                    continue
                try:
                    cid = await upsert_company(
                        conn, name=c["name"], ats=ats_type, slug=slug, careers_url=url,
                    )
                    if cid:
                        inserted += 1
                except Exception as e:
                    logger.debug("ai_upsert_failed", name=c["name"], err=str(e))
            await asyncio.sleep(0.3)

    await conn.close()
    return {"discovered": len(candidates), "ats_known": ats_known, "inserted": inserted}


# ════════════════════════════════════════════════════════════════════════════
#  Theme rotation (Redis-backed)
# ════════════════════════════════════════════════════════════════════════════

def _next_theme() -> str:
    """Get + advance the rotating theme index in Redis."""
    try:
        import redis
        r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        idx = r.incr("ai_disco:theme_idx") - 1
    except Exception:
        # Fallback: hash time-of-day
        from datetime import datetime
        idx = datetime.utcnow().hour
    return THEMES[idx % len(THEMES)]


# ════════════════════════════════════════════════════════════════════════════
#  Master runner + Celery task
# ════════════════════════════════════════════════════════════════════════════

async def _run_ai_discovery() -> dict:
    theme = _next_theme()
    logger.info("ai_disco_run", theme=theme[:80])
    candidates = await ask_claude_for_companies(theme)
    result = await _upsert_ai_companies(candidates)
    result["theme"] = theme[:100]
    logger.info(
        "ai_disco_done",
        theme=theme[:60],
        discovered=result["discovered"],
        ats_known=result["ats_known"],
        inserted=result["inserted"],
    )
    return result


@celery_app.task(
    name="app.workers.ai_company_discovery.discover_via_ai",
    soft_time_limit=900, max_retries=1,
)
def task_discover_via_ai() -> dict:
    return _run_async(_run_ai_discovery())
