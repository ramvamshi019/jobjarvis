"""
Discover hiring companies from top US VC portfolio pages.

VC-backed startups are almost universally hiring and almost universally use
a standard ATS (Greenhouse / Lever / Ashby) — so each one we discover here
plugs straight into our existing scan pipeline.

Sources (all public, no auth):
  • Sequoia, a16z, First Round, Bessemer, Greylock, Khosla, Founders Fund,
    General Catalyst, Lightspeed, NEA, Accel, Battery, Index Ventures,
    Founder Collective, IVP, USV  (16 VC portfolios)
  • TechCrunch funding RSS — every newly funded company in the last 24h
  • Wikipedia "List of Y Combinator companies" — backup massive seed

For each company we extract a name + a website link, then probe common
careers paths to find the ATS URL.  Already-known companies are de-duped
via the case-insensitive unique index on companies.name.

Schedule (celery beat):
  • VC portfolios sweep:  weekly Sunday 03:00 UTC
  • TechCrunch funding:   daily 06:00 UTC
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from urllib.parse import urlparse, urljoin
from typing import Optional

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_TIMEOUT = httpx.Timeout(45.0, connect=15.0)


# ════════════════════════════════════════════════════════════════════════════
#  1. VC Portfolio scraper
# ════════════════════════════════════════════════════════════════════════════
#
# Each entry: (vc_name, portfolio_url, ignore_link_pattern)
# - ignore_link_pattern: regex of URLs to NOT count as a portfolio company
#   (e.g. social media, the VC's own domain, etc).
VC_PORTFOLIOS: list[tuple[str, str, str]] = [
    ("Sequoia",        "https://www.sequoiacap.com/our-companies/",       r"sequoiacap\.com|twitter\.com|linkedin\.com|facebook\.com"),
    ("a16z",           "https://a16z.com/portfolio/",                      r"a16z\.com|twitter\.com|linkedin\.com|facebook\.com"),
    ("First Round",    "https://firstround.com/companies/",                r"firstround\.com|twitter\.com|linkedin\.com"),
    ("Bessemer",       "https://www.bvp.com/portfolio",                    r"bvp\.com|twitter\.com|linkedin\.com"),
    ("Greylock",       "https://greylock.com/portfolio/",                  r"greylock\.com|twitter\.com|linkedin\.com"),
    ("Khosla",         "https://www.khoslaventures.com/portfolio/",        r"khoslaventures\.com|twitter\.com|linkedin\.com"),
    ("Founders Fund",  "https://foundersfund.com/portfolio/",              r"foundersfund\.com|twitter\.com|linkedin\.com"),
    ("General Catalyst","https://www.generalcatalyst.com/portfolio",       r"generalcatalyst\.com|twitter\.com|linkedin\.com"),
    ("Lightspeed",     "https://lsvp.com/portfolio/",                      r"lsvp\.com|lightspeed|twitter\.com|linkedin\.com"),
    ("NEA",            "https://www.nea.com/portfolio",                    r"nea\.com|twitter\.com|linkedin\.com"),
    ("Accel",          "https://www.accel.com/companies",                  r"accel\.com|twitter\.com|linkedin\.com"),
    ("Battery",        "https://www.battery.com/our-companies/",           r"battery\.com|twitter\.com|linkedin\.com"),
    ("Index Ventures", "https://www.indexventures.com/companies/",         r"indexventures\.com|twitter\.com|linkedin\.com"),
    ("Founder Collective","https://foundercollective.com/portfolio/",      r"foundercollective\.com|twitter\.com|linkedin\.com"),
    ("IVP",            "https://www.ivp.com/portfolio/",                   r"ivp\.com|twitter\.com|linkedin\.com"),
    ("USV",            "https://www.usv.com/companies/",                   r"usv\.com|twitter\.com|linkedin\.com"),
]

# Regex to extract <a href="https://..."> outbound links
_LINK_RE = re.compile(
    r'<a[^>]*href="(https?://[^"]+)"[^>]*>([^<]{2,150})</a>',
    re.I,
)
# Generic noise patterns to skip
_NOISE_DOMAINS = re.compile(
    r"(twitter|facebook|instagram|linkedin|youtube|google\.com|crunchbase|"
    r"github|medium|substack|notion\.so|airtable|forms|mailto|tel:|"
    r"cdn-cgi|cookielaw|onetrust|hubspot|salesforce|amazon\.com|apple\.com)",
    re.I,
)


def _clean_company_name(text: str) -> str:
    """Strip whitespace + HTML noise from a link's text content."""
    text = re.sub(r"\s+", " ", text).strip()
    # Drop obvious section labels that aren't company names
    if text.lower() in {
        "learn more", "read more", "visit website", "view all",
        "next", "previous", "view profile", "more", "→",
    }:
        return ""
    if len(text) < 2 or len(text) > 200:
        return ""
    return text


async def _extract_links_from_html(html: str, ignore_pat: str) -> dict[str, tuple[str, str]]:
    """Shared parser: pull outbound links → (name, full_url) keyed by host."""
    ignore = re.compile(ignore_pat, re.I)
    found: dict[str, tuple[str, str]] = {}
    for m in _LINK_RE.finditer(html):
        link = m.group(1).strip()
        name = _clean_company_name(m.group(2))
        if not name:
            continue
        try:
            host = urlparse(link).netloc.lower()
        except Exception:
            continue
        if not host or ignore.search(link) or _NOISE_DOMAINS.search(host):
            continue
        host_clean = host.replace("www.", "")
        if host_clean in found:
            continue
        found[host_clean] = (name, link)
    return found


async def _scrape_via_playwright(vc_name: str, url: str, ignore_pat: str) -> list[tuple[str, str]]:
    """Render the page with Chromium so JS-rendered portfolios populate."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("playwright_missing", vc=vc_name)
        return []

    found: dict[str, tuple[str, str]] = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                # networkidle can time out on heavy pages — fall back to domcontentloaded
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    await browser.close()
                    return []

            # Scroll to bottom to trigger lazy loading
            try:
                for _ in range(6):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(800)
            except Exception:
                pass

            # Pull all anchor (href, text) pairs in JS
            try:
                pairs = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]'))
                        .map(a => ({
                            href: a.href,
                            text: (a.innerText || a.textContent || '').trim()
                        }))
                        .filter(p => p.href.startsWith('http') && p.text && p.text.length < 200)
                    """
                )
            except Exception:
                pairs = []

            await browser.close()
    except Exception as e:
        logger.warning("playwright_scrape_failed", vc=vc_name, err=str(e))
        return []

    ignore = re.compile(ignore_pat, re.I)
    for p_ in pairs or []:
        link = p_.get("href", "")
        name = _clean_company_name(p_.get("text", ""))
        if not name:
            continue
        try:
            host = urlparse(link).netloc.lower()
        except Exception:
            continue
        if not host or ignore.search(link) or _NOISE_DOMAINS.search(host):
            continue
        host_clean = host.replace("www.", "")
        if host_clean in found:
            continue
        found[host_clean] = (name, link)

    return [(n, u) for n, u in found.values()]


async def scrape_vc_portfolio(
    client: httpx.AsyncClient, vc_name: str, url: str, ignore_pat: str
) -> list[tuple[str, str]]:
    """
    Return (company_name, website_url) tuples from a VC portfolio page.
    Tries static HTML first; if yield < 20 companies, falls back to Playwright
    JS-rendering (most modern VC sites are React SPAs).
    """
    # First try: cheap static HTML
    try:
        r = await client.get(url, headers=_HEADERS, follow_redirects=True)
        if r.status_code == 200:
            found = await _extract_links_from_html(r.text, ignore_pat)
            static_out = [(n, u) for n, u in found.values()]
        else:
            static_out = []
            logger.info("vc_portfolio_static_status", vc=vc_name, code=r.status_code)
    except Exception as e:
        static_out = []
        logger.info("vc_portfolio_static_failed", vc=vc_name, err=str(e))

    if len(static_out) >= 20:
        logger.info("vc_portfolio_static_ok", vc=vc_name, count=len(static_out))
        return static_out

    # Static yielded too few — likely an SPA. Render with Playwright.
    logger.info("vc_portfolio_fallback_to_js", vc=vc_name, static_count=len(static_out))
    js_out = await _scrape_via_playwright(vc_name, url, ignore_pat)
    if len(js_out) > len(static_out):
        logger.info("vc_portfolio_js_ok", vc=vc_name, count=len(js_out))
        return js_out
    return static_out


# ════════════════════════════════════════════════════════════════════════════
#  2. TechCrunch funding RSS — every newly funded company in last 24h
# ════════════════════════════════════════════════════════════════════════════

TC_FUNDING_FEEDS = [
    "https://techcrunch.com/category/venture/feed/",
    "https://techcrunch.com/category/startups/feed/",
]


async def scrape_techcrunch_funding(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Parse TC's RSS feeds, extract company names + websites."""
    out: dict[str, tuple[str, str]] = {}

    for feed_url in TC_FUNDING_FEEDS:
        try:
            r = await client.get(feed_url, headers=_HEADERS)
            if r.status_code != 200:
                continue
            xml = r.text
        except Exception as e:
            logger.warning("tc_feed_failed", url=feed_url, err=str(e))
            continue

        # Match items: <item> ... <title>...</title> ... <description>...</description>
        for item in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
            block = item.group(1)
            title_m = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.+?)(?:\]\]>)?\s*</title>", block, re.DOTALL)
            desc_m = re.search(r"<description>\s*(?:<!\[CDATA\[)?(.+?)(?:\]\]>)?\s*</description>", block, re.DOTALL)
            if not title_m:
                continue
            title = title_m.group(1).strip()
            desc = (desc_m.group(1) if desc_m else "").strip()

            # Match "<CompanyName> raises $X" or "<CompanyName> secures $X" patterns
            funding_m = re.search(
                r"^([A-Z][\w\.\-&\s]{1,60})\s+(?:raises|secures|closes|nabs|lands|"
                r"announces|gets|bags|adds|locks down|grabs|picks up)\s+\$",
                title,
            )
            if not funding_m:
                continue
            name = funding_m.group(1).strip()
            if len(name) < 2 or len(name) > 80:
                continue

            # Try to extract company website from description (often linked first)
            link_m = re.search(r'href="(https?://[^"]+)"', desc)
            link = link_m.group(1) if link_m else ""
            if link and _NOISE_DOMAINS.search(link):
                link = ""

            # Skip duplicates
            key = name.lower()
            if key in out:
                continue
            out[key] = (name, link or "")

    pairs = [(n, u) for (n, u) in out.values()]
    logger.info("techcrunch_funding_parsed", count=len(pairs))
    return pairs


# ════════════════════════════════════════════════════════════════════════════
#  Common: probe each company URL for /careers, detect ATS, upsert
# ════════════════════════════════════════════════════════════════════════════

async def _probe_careers_url(
    client: httpx.AsyncClient, company_url: str
) -> Optional[str]:
    """
    Given a company's homepage URL, try common careers paths and return the
    URL of an ATS-hosted careers page if found.  Otherwise returns the
    homepage `/careers` URL if it exists, or None.
    """
    if not company_url:
        return None
    try:
        parsed = urlparse(company_url)
        if not parsed.netloc:
            return None
        base = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None

    paths = ["/careers", "/jobs", "/about/careers", "/company/careers", "/work-with-us"]
    for path in paths:
        url = f"{base}{path}"
        try:
            r = await client.get(url, headers=_HEADERS, follow_redirects=True, timeout=10.0)
            if r.status_code != 200:
                continue
            text = r.text[:80_000]
            # Search for embedded ATS URLs
            for ats_pat in [
                r"https?://[^\"'\s>]+(?:greenhouse\.io|lever\.co|ashbyhq\.com|"
                r"workable\.com|smartrecruiters\.com|myworkdayjobs\.com|"
                r"icims\.com|teamtailor\.com|bamboohr\.com|jobvite\.com)"
                r"[^\"'\s>]*",
            ]:
                m = re.search(ats_pat, text, re.I)
                if m:
                    return m.group(0)
            # No embedded ATS found — return this careers page itself
            return url
        except Exception:
            continue
    return None


async def _upsert_pairs(
    pairs: list[tuple[str, str]], source: str,
    client: httpx.AsyncClient,
) -> dict:
    """For each (name, url) probe for careers page + ATS, then upsert."""
    if not _DISCOVERY_AVAILABLE:
        return {"discovered": len(pairs), "ats_known": 0, "inserted": 0,
                "error": "discovery_lib not importable"}

    conn = await asyncpg.connect(_DB_DSN)
    ats_known = inserted = 0
    # Cap to keep run time reasonable
    capped = pairs[:600]

    # Probe in parallel batches of 10
    for i in range(0, len(capped), 10):
        batch = capped[i:i+10]
        careers = await asyncio.gather(
            *[_probe_careers_url(client, url) for (_n, url) in batch],
            return_exceptions=True,
        )
        for (name, _origin_url), careers_url in zip(batch, careers):
            if not isinstance(careers_url, str) or not careers_url:
                continue
            match = detect_ats(careers_url)
            if match:
                ats_type, slug = match
                ats_known += 1
            else:
                ats_type = "unknown"
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower())[:60].strip("-")
            if not slug:
                continue
            try:
                cid = await upsert_company(
                    conn, name=name, ats=ats_type, slug=slug, careers_url=careers_url,
                )
                if cid:
                    inserted += 1
            except Exception as e:
                logger.debug("upsert_failed", name=name, err=str(e))
        # be polite between batches
        await asyncio.sleep(0.5)

    await conn.close()
    logger.info(
        "vc_companies_upserted", source=source,
        discovered=len(pairs), ats_known=ats_known, inserted=inserted,
    )
    return {"discovered": len(pairs), "ats_known": ats_known, "inserted": inserted}


# ════════════════════════════════════════════════════════════════════════════
#  Runners
# ════════════════════════════════════════════════════════════════════════════

async def _run_vc_portfolios() -> dict:
    """Scrape all 16 VC portfolios, probe each company's careers page, upsert."""
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS,
    ) as client:
        all_pairs: list[tuple[str, str]] = []
        for vc_name, url, ignore_pat in VC_PORTFOLIOS:
            try:
                pairs = await scrape_vc_portfolio(client, vc_name, url, ignore_pat)
                all_pairs.extend(pairs)
                await asyncio.sleep(2.0)   # politeness between VC sites
            except Exception as e:
                logger.exception("vc_scrape_fail", vc=vc_name, err=str(e))

        # Dedup by domain across all VCs
        by_host: dict[str, tuple[str, str]] = {}
        for name, url in all_pairs:
            try:
                h = urlparse(url).netloc.lower().replace("www.", "")
                if h and h not in by_host:
                    by_host[h] = (name, url)
            except Exception:
                continue
        unique = list(by_host.values())
        logger.info("vc_portfolios_total", scraped=len(all_pairs), unique=len(unique))

        result = await _upsert_pairs(unique, source="vc_portfolios", client=client)
        result["vcs_scraped"] = len(VC_PORTFOLIOS)
        return result


async def _run_techcrunch_funding() -> dict:
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS,
    ) as client:
        pairs = await scrape_techcrunch_funding(client)
        return await _upsert_pairs(pairs, source="techcrunch_funding", client=client)


# ════════════════════════════════════════════════════════════════════════════
#  Celery tasks
# ════════════════════════════════════════════════════════════════════════════

@celery_app.task(
    name="app.workers.vc_portfolio_discovery.discover_vc_portfolios",
    soft_time_limit=3600, max_retries=1,
)
def task_discover_vc_portfolios(): return _run_async(_run_vc_portfolios())

@celery_app.task(
    name="app.workers.vc_portfolio_discovery.discover_techcrunch_funding",
    soft_time_limit=900, max_retries=1,
)
def task_discover_techcrunch_funding(): return _run_async(_run_techcrunch_funding())
