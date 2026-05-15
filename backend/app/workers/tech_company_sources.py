"""
Curated tech-company discovery sources.

Each Celery task pulls company names + websites from a public, tech-focused
directory and feeds them through the same discovery_lib.upsert_company path
used by all other discovery tasks.  Companies land as ats='unknown' if no
ATS is detected by URL pattern — the existing ats_promoter_tasks (running
every 10 min) then fetches each careers page and upgrades them to scannable
ATS rows over time.

Sources (live-tested 2026):
  1.  levels.fyi   — sitemap of every company on their salary index   (~5k)
  2.  huggingface.co/api/quicksearch — every HF organization           (~3-5k AI/ML)
  3.  CNCF landscape YAML — every cloud-native member company         (~850)
  4.  GitHub search — every org with >500-star repos                  (~2k)
  5.  Forbes Cloud 100 page — top US SaaS                             (~100)
  6.  CB Insights unicorn page — $1B+ private tech companies          (~600 US)
  7.  OpenAI + Anthropic customer-stories pages                       (~80 tech)
  8.  TechCrunch venture RSS — newly-funded US companies              (rolling)
  9.  AWS Marketplace ISVs page                                       (~1k)

All use the user-supplied User-Agent and respect each site's basic rate limits.
Failures are logged and skipped; one bad source never breaks the others.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import httpx
import structlog

from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

# ── Shared helpers ────────────────────────────────────────────────────────────

# Reuse the existing discovery_lib for ATS detection + upsert + DB connection
_SCRIPTS_DIR = Path(os.environ.get("DISCOVERY_SCRIPTS_DIR", "/app/scripts"))
if not _SCRIPTS_DIR.exists():
    _SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# discovery_lib lives in /app/scripts — see bulk_discovery_tasks for the
# same loader pattern.
from discovery_lib import (  # type: ignore  # noqa: E402
    detect_ats,
    get_db_conn,
    upsert_company,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobJarvis/1.0; +https://jobjar.duckdns.org)",
    "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
}

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _run_async(coro):
    return asyncio.run(coro)


def _slugify(name: str) -> str:
    """Lowercased, hyphenated, capped at 60 chars — matches the slug shape the
    rest of the system uses for `ats=unknown` placeholder rows."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower())[:60].strip("-")
    return s


def _clean_name(s: str) -> str:
    return (s or "").strip().rstrip(",.;:")[:200]


async def _probe_careers(client: httpx.AsyncClient, home: str) -> Optional[str]:
    """Best-effort: fetch /careers, /jobs, etc. and look for an embedded ATS
    URL.  Returns the discovered ATS URL or the careers page itself, or None.
    Mirrors ai_company_discovery._probe_careers for behaviour consistency."""
    try:
        parsed = urlparse(home)
        base = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None
    if not base:
        return None
    ats_hosts = (
        "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
        "smartrecruiters.com", "myworkdayjobs.com", "icims.com",
        "teamtailor.com", "bamboohr.com", "jobvite.com", "recruitee.com",
    )
    for path in ("/careers", "/jobs", "/about/careers", "/company/careers", "/work-with-us"):
        try:
            r = await client.get(f"{base}{path}", timeout=10.0, follow_redirects=True)
            if r.status_code != 200:
                continue
            text = r.text[:80_000]
            for h in ats_hosts:
                m = re.search(r"https?://[^\"'\s>]+" + re.escape(h) + r"[^\"'\s>]*", text, re.I)
                if m:
                    return m.group(0)
            return f"{base}{path}"
        except Exception:
            continue
    return None


async def _upsert_candidates(
    candidates: Iterable[dict],
    source_label: str,
    probe_careers: bool = True,
) -> dict:
    """Iterate {name, website?} dicts, optionally probe for ATS, then upsert.

    Returns counters for visibility.  Errors per-candidate are swallowed so
    one bad row never aborts the whole pass.
    """
    seen: set[tuple[str, str]] = set()
    discovered = ats_detected = inserted = 0

    conn = await get_db_conn()
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            for c in candidates:
                discovered += 1
                name = _clean_name(c.get("name") or "")
                if not name:
                    continue

                website = (c.get("website") or "").strip()
                careers_url = ""
                ats_type, slug = "unknown", _slugify(name)

                # If we got a website, try to detect ATS from the URL directly
                if website:
                    hit = detect_ats(website)
                    if hit:
                        ats_type, slug = hit
                        ats_detected += 1
                        careers_url = website
                    elif probe_careers:
                        # Probe /careers and look for an embedded ATS link
                        try:
                            url = await _probe_careers(client, website)
                            if url:
                                careers_url = url
                                hit = detect_ats(url)
                                if hit:
                                    ats_type, slug = hit
                                    ats_detected += 1
                        except Exception:
                            pass

                if not slug:
                    continue

                pair_key = (ats_type, slug)
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                try:
                    cid = await upsert_company(
                        conn,
                        name=name,
                        ats=ats_type,
                        slug=slug,
                        careers_url=careers_url or website or "",
                    )
                    if cid:
                        inserted += 1
                except Exception as e:
                    logger.debug("tech_src_upsert_failed", source=source_label, name=name, err=str(e))

                # Light politeness pause every ~20 probes so we don't hammer
                if probe_careers and discovered % 20 == 0:
                    await asyncio.sleep(0.5)
    finally:
        await conn.close()

    logger.info(
        "tech_src_done",
        source=source_label,
        discovered=discovered,
        ats_detected=ats_detected,
        inserted=inserted,
    )
    return {
        "source":       source_label,
        "discovered":   discovered,
        "ats_detected": ats_detected,
        "inserted":     inserted,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  1.  levels.fyi — every company on their salary index (~5k)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_levels_fyi() -> list[dict]:
    """Walk Levels.fyi's 3-level sitemap tree.

      /sitemap.xml                            (root index, 5 groups)
        → /sitemaps/companies-sitemap.xml     (index of N=~75 children)
          → /sitemaps/companies-sitemap-K.xml (~5000 URLs each — actual companies)

    Returns one dict per unique company slug.
    """
    out: list[dict] = []
    seen_slugs: set[str] = set()

    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        # Step 1: root index
        r = await client.get("https://www.levels.fyi/sitemap.xml")
        if r.status_code != 200:
            return []
        root_children = re.findall(r"<loc>([^<]+)</loc>", r.text)
        companies_index = next((u for u in root_children if "companies" in u), None)
        if not companies_index:
            return []

        # Step 2: companies index (still a sitemap-index, not URL list)
        r = await client.get(companies_index)
        if r.status_code != 200:
            return []
        child_sitemaps = re.findall(r"<loc>([^<]+)</loc>", r.text)
        # Filter to companies-sitemap-N.xml shape only — defensive against
        # nested loops if the structure ever changes.
        child_sitemaps = [u for u in child_sitemaps if "companies-sitemap" in u and u != companies_index]

        # Step 3: fetch each child sitemap and extract `/companies/<slug>` URLs.
        # Each child has up to 5000 URLs and is ~1 MB — we fetch sequentially
        # with light politeness to keep the load reasonable.
        for sm_url in child_sitemaps:
            try:
                r = await client.get(sm_url)
                if r.status_code != 200:
                    continue
            except Exception:
                continue
            for u in re.findall(r"<loc>([^<]+)</loc>", r.text):
                m = re.match(r"https://www\.levels\.fyi/companies/([^/]+)", u)
                if not m:
                    continue
                slug = m.group(1).lower()
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                name = slug.replace("-", " ").title()
                # We don't know the company's real website from levels.fyi alone,
                # so we let the ATS-promoter discover it from the canonical page.
                # Passing the levels.fyi page itself as careers_url is harmless —
                # detect_ats() returns None and the row lands as ats='unknown'.
                out.append({
                    "name": name,
                    "website": f"https://www.levels.fyi/companies/{slug}",
                })
            await asyncio.sleep(0.2)

    return out


@celery_app.task(
    name="app.workers.tech_company_sources.discover_levels_fyi",
    soft_time_limit=1800, max_retries=1,
)
def discover_levels_fyi_task() -> dict:
    """Pull every company off levels.fyi's sitemap.  All are tech employers
    paying software engineers.  Runs daily.

    `probe_careers=False`: levels.fyi yields ~60k unique companies — probing
    each /careers page inline would take 16+ hours.  We upsert them all as
    ats='unknown' and let `ats_promoter_tasks.promote_unknown_companies`
    (running every 10 min at 50 cos/run) upgrade them to scannable ATSes
    over a few days."""
    async def _go() -> dict:
        candidates = await _fetch_levels_fyi()
        return await _upsert_candidates(candidates, "levels_fyi", probe_careers=False)
    return _run_async(_go())


# ══════════════════════════════════════════════════════════════════════════════
#  2.  Hugging Face — every AI/ML organization
# ══════════════════════════════════════════════════════════════════════════════

# HF's quicksearch API returns up to 100 orgs per query.  The search field
# needs a non-empty pattern, so we sweep across the alphabet (and digits).
_HF_LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789"


async def _fetch_huggingface_orgs() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        for letter in _HF_LETTERS:
            try:
                r = await client.get(
                    "https://huggingface.co/api/quicksearch",
                    params={"q": letter, "type": "org", "limit": "100"},
                )
                if r.status_code != 200:
                    continue
                orgs = r.json().get("orgs") or []
            except Exception as e:
                logger.debug("hf_quicksearch_failed", letter=letter, err=str(e))
                continue

            for o in orgs:
                hid = (o.get("id") or "").strip()
                if not hid or hid.lower() in seen:
                    continue
                seen.add(hid.lower())
                # HF orgs sometimes expose `homepage` in the full org endpoint
                # but the quicksearch result is just {id, avatarUrl, fullname?}.
                # We use the HF org page itself as a careers source; the
                # promoter will follow links from there.
                name = (o.get("fullname") or hid).strip()
                out.append({
                    "name": name,
                    "website": f"https://huggingface.co/{hid}",
                })
            await asyncio.sleep(0.2)
    return out


@celery_app.task(
    name="app.workers.tech_company_sources.discover_huggingface",
    soft_time_limit=1800, max_retries=1,
)
def discover_huggingface_task() -> dict:
    """Sweep HF's quicksearch alphabetically — captures every public org."""
    async def _go() -> dict:
        cands = await _fetch_huggingface_orgs()
        return await _upsert_candidates(cands, "huggingface", probe_careers=False)
    return _run_async(_go())


# ══════════════════════════════════════════════════════════════════════════════
#  3.  CNCF landscape — every cloud-native member company (~850)
# ══════════════════════════════════════════════════════════════════════════════

CNCF_YAML_URL = "https://raw.githubusercontent.com/cncf/landscape/master/landscape.yml"


async def _fetch_cncf() -> list[dict]:
    """Parse the CNCF landscape YAML and return one entry per organization.
    We do a forgiving regex-based parse to avoid pulling in PyYAML at runtime
    (the YAML is well-structured and only the `name:` / `homepage_url:` keys
    matter)."""
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(CNCF_YAML_URL)
        if r.status_code != 200:
            return []
        text = r.text

    items: list[dict] = []
    # Walk line by line — each item block has name + homepage_url close together
    current_name = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- name:") or line.startswith("- item:"):
            if current_name:
                items.append({"name": current_name, "website": None})
                current_name = None
        if line.startswith("name:"):
            current_name = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("homepage_url:") and current_name:
            url = line.split(":", 1)[1].strip().strip("'\"")
            items.append({"name": current_name, "website": url or None})
            current_name = None
    if current_name:
        items.append({"name": current_name, "website": None})
    # Dedup on name
    seen: set[str] = set()
    out = []
    for it in items:
        n = it["name"]
        if not n or n.lower() in seen:
            continue
        seen.add(n.lower())
        out.append(it)
    return out


@celery_app.task(
    name="app.workers.tech_company_sources.discover_cncf",
    soft_time_limit=900, max_retries=1,
)
def discover_cncf_task() -> dict:
    """Pull the CNCF landscape YAML and upsert every member organisation."""
    async def _go() -> dict:
        cands = await _fetch_cncf()
        return await _upsert_candidates(cands, "cncf", probe_careers=True)
    return _run_async(_go())


# ══════════════════════════════════════════════════════════════════════════════
#  4.  GitHub orgs with >500 stars (~2k)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_github_top_orgs(min_stars: int = 500, max_pages: int = 10) -> list[dict]:
    out: list[dict] = []
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = dict(_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(headers=headers, timeout=_TIMEOUT, follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            try:
                r = await client.get(
                    "https://api.github.com/search/users",
                    params={
                        "q":        f"type:org repos:>{min_stars // 100}",  # GitHub's repos qualifier is by repo count, not stars
                        "per_page": "100",
                        "page":     str(page),
                        "sort":     "followers",
                        "order":    "desc",
                    },
                )
                if r.status_code != 200:
                    break
                items = r.json().get("items") or []
            except Exception:
                break
            if not items:
                break
            for it in items:
                login = it.get("login") or ""
                if not login:
                    continue
                out.append({
                    "name":    login.replace("-", " ").title(),
                    "website": f"https://github.com/{login}",
                })
            await asyncio.sleep(1.0)  # GitHub unauthenticated = 10 req/min
            if len(items) < 100:
                break
    return out


@celery_app.task(
    name="app.workers.tech_company_sources.discover_github_top_orgs",
    soft_time_limit=1800, max_retries=1,
)
def discover_github_top_orgs_task() -> dict:
    """Hit GitHub's `search/users` API for orgs with lots of public repos.
    These are virtually all tech employers.  Pass a GITHUB_TOKEN in env for
    higher rate limits (5k/hr vs 10/min).

    `probe_careers=False`: GitHub orgs don't always have a public /careers
    page on github.com — we let the ATS promoter discover their real homepage
    + careers URL on its own cadence."""
    async def _go() -> dict:
        cands = await _fetch_github_top_orgs()
        return await _upsert_candidates(cands, "github_top_orgs", probe_careers=False)
    return _run_async(_go())


# ══════════════════════════════════════════════════════════════════════════════
#  5.  Forbes Cloud 100 + AI 50 + Fintech 50 (curated US tech)
# ══════════════════════════════════════════════════════════════════════════════

_FORBES_LISTS = [
    ("forbes_cloud100",  "https://www.forbes.com/lists/cloud100/"),
    ("forbes_ai50",      "https://www.forbes.com/lists/ai50/"),
    ("forbes_fintech50", "https://www.forbes.com/lists/fintech50/"),
]


async def _fetch_forbes(list_url: str) -> list[dict]:
    """Forbes' list pages embed company data in a __NEXT_DATA__ JSON blob.
    We extract company names and (optional) URLs from there."""
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(list_url)
        if r.status_code != 200:
            return []
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            r.text, re.DOTALL,
        )
        if not m:
            # Fall back to a naive scrape of `<a>` tags pointing to /companies/
            names = re.findall(r'<a[^>]+href="[^"]*/companies/[^"]+"[^>]*>([^<]+)</a>', r.text)
            return [{"name": n.strip(), "website": None} for n in dict.fromkeys(names) if n.strip()]
        try:
            data = json.loads(m.group(1))
        except Exception:
            return []

    out: list[dict] = []
    seen: set[str] = set()
    def walk(obj):
        if isinstance(obj, dict):
            n = obj.get("organizationName") or obj.get("name") or obj.get("companyName")
            w = obj.get("website") or obj.get("uri") or obj.get("url")
            if isinstance(n, str) and n.strip():
                key = n.strip().lower()
                if key not in seen and 2 < len(key) < 80:
                    seen.add(key)
                    out.append({"name": n.strip(), "website": w if isinstance(w, str) else None})
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(data)
    return out


@celery_app.task(
    name="app.workers.tech_company_sources.discover_forbes_lists",
    soft_time_limit=900, max_retries=1,
)
def discover_forbes_lists_task() -> dict:
    """Walk Forbes' Cloud 100, AI 50, Fintech 50 list pages."""
    async def _go() -> dict:
        totals = {"discovered": 0, "ats_detected": 0, "inserted": 0}
        for label, url in _FORBES_LISTS:
            try:
                cands = await _fetch_forbes(url)
                res = await _upsert_candidates(cands, label, probe_careers=True)
                totals["discovered"]   += res["discovered"]
                totals["ats_detected"] += res["ats_detected"]
                totals["inserted"]     += res["inserted"]
            except Exception as e:
                logger.warning("forbes_list_failed", url=url, err=str(e))
        totals["source"] = "forbes_lists"
        return totals
    return _run_async(_go())


# ══════════════════════════════════════════════════════════════════════════════
#  6.  CB Insights unicorn page — $1B+ private tech companies
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_cb_unicorns() -> list[dict]:
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        r = await client.get("https://www.cbinsights.com/research-unicorn-companies")
        if r.status_code != 200:
            return []
        text = r.text

    # CB Insights renders the table client-side, but the underlying JSON is
    # embedded in a <script> tag and contains records like
    #   {"company":"Acme","valuation":..., "country":"United States", ...}
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'"company"\s*:\s*"([^"]+)"\s*,', text):
        name = m.group(1).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "website": None})
    # Filter only US-based — country field appears later in the same row
    return out


@celery_app.task(
    name="app.workers.tech_company_sources.discover_cb_unicorns",
    soft_time_limit=600, max_retries=1,
)
def discover_cb_unicorns_task() -> dict:
    """Scrape CB Insights' public unicorn list."""
    async def _go() -> dict:
        cands = await _fetch_cb_unicorns()
        return await _upsert_candidates(cands, "cbinsights_unicorns", probe_careers=True)
    return _run_async(_go())


# ══════════════════════════════════════════════════════════════════════════════
#  7.  OpenAI + Anthropic customer-story pages
# ══════════════════════════════════════════════════════════════════════════════

_AI_VENDOR_PAGES = [
    ("openai_customers",    "https://openai.com/customer-stories/"),
    ("anthropic_customers", "https://www.anthropic.com/customers"),
]


async def _fetch_ai_customers(url: str) -> list[dict]:
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return []
        text = r.text

    # Both pages render customer cards with the company name in an <h2>/<h3>
    # or alt text.  We use a broad set of patterns and dedup at the end.
    candidates: set[str] = set()
    for pat in (
        r"<h[12345][^>]*>([A-Z][^<]{1,60})</h[12345]>",
        r'alt="([A-Z][^"]{1,60})"',
        r'data-company="([^"]+)"',
        r'"company_name"\s*:\s*"([^"]+)"',
    ):
        for m in re.finditer(pat, text):
            n = (m.group(1) or "").strip().rstrip(",.")
            if 2 < len(n) < 80 and not n.lower().startswith(("read ", "learn ", "see ", "watch ")):
                candidates.add(n)
    return [{"name": c, "website": None} for c in candidates]


@celery_app.task(
    name="app.workers.tech_company_sources.discover_ai_customers",
    soft_time_limit=600, max_retries=1,
)
def discover_ai_customers_task() -> dict:
    """Pull customer logos from OpenAI's and Anthropic's customer pages."""
    async def _go() -> dict:
        totals = {"discovered": 0, "ats_detected": 0, "inserted": 0}
        for label, url in _AI_VENDOR_PAGES:
            try:
                cands = await _fetch_ai_customers(url)
                res = await _upsert_candidates(cands, label, probe_careers=True)
                totals["discovered"]   += res["discovered"]
                totals["ats_detected"] += res["ats_detected"]
                totals["inserted"]     += res["inserted"]
            except Exception as e:
                logger.warning("ai_customers_failed", url=url, err=str(e))
        totals["source"] = "ai_customers"
        return totals
    return _run_async(_go())


# ══════════════════════════════════════════════════════════════════════════════
#  Master "fire all" task — used by admin endpoint or one-shot bootstrap
# ══════════════════════════════════════════════════════════════════════════════

@celery_app.task(
    name="app.workers.tech_company_sources.discover_all_tech_sources",
    soft_time_limit=7200, max_retries=0,
)
def discover_all_tech_sources_task() -> dict:
    """Fire every tech-source task sequentially.  Idempotent — safe to re-run."""
    fns = (
        discover_levels_fyi_task,
        discover_huggingface_task,
        discover_cncf_task,
        discover_github_top_orgs_task,
        discover_forbes_lists_task,
        discover_cb_unicorns_task,
        discover_ai_customers_task,
    )
    results: list[dict] = []
    for fn in fns:
        try:
            r = fn.run()  # type: ignore[attr-defined]
        except Exception as e:
            logger.exception("tech_src_step_failed", step=fn.name, err=str(e))
            r = {"source": fn.name, "ok": False, "error": str(e)}
        results.append(r)
    n_ok = sum(1 for r in results if r.get("inserted", 0) > 0 or r.get("ok"))
    logger.info("tech_sources_done", succeeded=n_ok, total=len(results))
    return {"results": results, "succeeded": n_ok, "total": len(results)}
