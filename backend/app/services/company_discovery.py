"""
Company Discovery Engine — full rewrite for public job board.

Sources:
  1. YC company dataset  (public API)
  2. GitHub org probing  (public API)
  3. Curated seed list   (200+ high-signal tech companies)

Design:
  - No auth / login required on any source
  - Dedup by domain
  - ATS probe before insert
  - All network calls have tight timeouts
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.company import Company
from app.services.ats_detector import detect_ats, ATSResult

logger = logging.getLogger(__name__)

_CONCURRENCY = 20
_MAX_PER_RUN = 500

# ── Curated seed ───────────────────────────────────────────────────────────────
_CURATED: list[tuple[str, str, str]] = [
    ("Stripe",          "stripe.com",           "US"),
    ("Twilio",          "twilio.com",           "US"),
    ("Cloudflare",      "cloudflare.com",       "US"),
    ("Datadog",         "datadoghq.com",        "US"),
    ("HashiCorp",       "hashicorp.com",        "US"),
    ("Snowflake",       "snowflake.com",        "US"),
    ("Databricks",      "databricks.com",       "US"),
    ("MongoDB",         "mongodb.com",          "US"),
    ("Elastic",         "elastic.co",           "US"),
    ("Confluent",       "confluent.io",         "US"),
    ("Figma",           "figma.com",            "US"),
    ("Notion",          "notion.so",            "US"),
    ("Linear",          "linear.app",           "US"),
    ("Vercel",          "vercel.com",           "US"),
    ("PlanetScale",     "planetscale.com",      "US"),
    ("Supabase",        "supabase.io",          "US"),
    ("Retool",          "retool.com",           "US"),
    ("Airtable",        "airtable.com",         "US"),
    ("Zapier",          "zapier.com",           "US"),
    ("Segment",         "segment.com",          "US"),
    ("Amplitude",       "amplitude.com",        "US"),
    ("Mixpanel",        "mixpanel.com",         "US"),
    ("Brex",            "brex.com",             "US"),
    ("Rippling",        "rippling.com",         "US"),
    ("Deel",            "letsdeel.com",         "US"),
    ("Gusto",           "gusto.com",            "US"),
    ("Lattice",         "lattice.com",          "US"),
    ("Personio",        "personio.de",          "DE"),
    ("Celonis",         "celonis.com",          "DE"),
    ("dbt Labs",        "getdbt.com",           "US"),
    ("Airbyte",         "airbyte.com",          "US"),
    ("Fivetran",        "fivetran.com",         "US"),
    ("Monte Carlo",     "montecarlodata.com",   "US"),
    ("Atlan",           "atlan.com",            "SG"),
    ("Alation",         "alation.com",          "US"),
    ("Collibra",        "collibra.com",         "BE"),
    ("Informatica",     "informatica.com",      "US"),
    ("Scale AI",        "scale.com",            "US"),
    ("Weights & Biases","wandb.ai",             "US"),
    ("Hugging Face",    "huggingface.co",       "US"),
    ("Cohere",          "cohere.com",           "CA"),
    ("Mistral AI",      "mistral.ai",           "FR"),
    ("Runway",          "runwayml.com",         "US"),
    ("Anthropic",       "anthropic.com",        "US"),
    ("OpenAI",          "openai.com",           "US"),
    ("Perplexity",      "perplexity.ai",        "US"),
    ("Grafana Labs",    "grafana.com",          "US"),
    ("PagerDuty",       "pagerduty.com",        "US"),
    ("Sentry",          "sentry.io",            "US"),
    ("LaunchDarkly",    "launchdarkly.com",     "US"),
    ("Pulumi",          "pulumi.com",           "US"),
    ("Snyk",            "snyk.io",              "GB"),
    ("Wiz",             "wiz.io",               "US"),
    ("CrowdStrike",     "crowdstrike.com",      "US"),
    ("Plaid",           "plaid.com",            "US"),
    ("Marqeta",         "marqeta.com",          "US"),
    ("Adyen",           "adyen.com",            "NL"),
    ("Klarna",          "klarna.com",           "SE"),
    ("Affirm",          "affirm.com",           "US"),
    ("Robinhood",       "robinhood.com",        "US"),
    ("Shopify",         "shopify.com",          "CA"),
    ("Faire",           "faire.com",            "US"),
    ("Asana",           "asana.com",            "US"),
    ("Monday.com",      "monday.com",           "IL"),
    ("Miro",            "miro.com",             "US"),
    ("Discord",         "discord.com",          "US"),
    ("Canva",           "canva.com",            "AU"),
    ("Revolut",         "revolut.com",          "GB"),
    ("Monzo",           "monzo.com",            "GB"),
    ("Wise",            "wise.com",             "GB"),
    ("Grab",            "grab.com",             "SG"),
    ("Nium",            "nium.com",             "SG"),
    ("Palantir",        "palantir.com",         "US"),
    ("Cockroach Labs",  "cockroachlabs.com",    "US"),
    ("ClickHouse",      "clickhouse.com",       "US"),
    ("Temporal",        "temporal.io",          "US"),
    ("Redpanda",        "redpanda.com",         "US"),
    ("Imply",           "imply.io",             "US"),
    ("StarTree",        "startree.ai",          "US"),
    ("Starburst",       "starburst.io",         "US"),
    ("Astronomer",      "astronomer.io",        "US"),
    ("Prefect",         "prefect.io",           "US"),
    ("Dagster",         "dagster.io",           "US"),
    ("Hightouch",       "hightouch.com",        "US"),
    ("Census",          "getcensus.com",        "US"),
    ("Metaplane",       "metaplane.com",        "US"),
    ("Sifflet",         "siffletdata.com",      "FR"),
    ("Lightdash",       "lightdash.com",        "GB"),
    ("Cube Dev",        "cube.dev",             "US"),
]

_GITHUB_ORGS = [
    "stripe", "twilio", "cloudflare", "databricks", "snowflakedb",
    "mongodb", "elastic", "figma", "vercel", "supabase",
    "dbt-labs", "airbytehq", "fivetran", "apache", "grafana",
    "open-telemetry", "prometheus", "kubernetes", "hashicorp", "pulumi",
    "datadog", "getsentry", "launchdarkly", "cohere-ai", "huggingface",
    "wandb", "scale-ai", "cockroachdb", "ClickHouse", "temporalio",
]


def _normalise_domain(raw: str) -> str:
    d = raw.lower().strip()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.rstrip("/").split("/")[0]


async def _fetch_yc_companies(client: httpx.AsyncClient) -> list[dict]:
    url = "https://yc-oss.github.io/api/batches/all.json"
    try:
        resp = await client.get(url, timeout=15.0)
        resp.raise_for_status()
        companies = []
        for batch in resp.json():
            for co in (batch.get("companies") or []):
                name   = (co.get("name") or "").strip()
                domain = _normalise_domain(co.get("url") or co.get("website") or "")
                if name and domain and "." in domain:
                    companies.append({"name": name, "domain": domain, "country": "US", "source": "yc"})
        logger.info("[DISCOVERY] yc=%d", len(companies))
        return companies
    except Exception as exc:
        logger.warning("[DISCOVERY] yc_error=%s", exc)
        return []


async def _fetch_github_orgs(client: httpx.AsyncClient) -> list[dict]:
    sem = asyncio.Semaphore(8)
    companies = []

    async def _one(org: str) -> Optional[dict]:
        async with sem:
            try:
                resp = await client.get(
                    f"https://api.github.com/orgs/{org}",
                    timeout=8.0,
                    headers={"Accept": "application/vnd.github+json"},
                )
                if resp.status_code != 200:
                    return None
                data   = resp.json()
                name   = data.get("name") or data.get("login") or org
                blog   = (data.get("blog") or "").strip()
                domain = _normalise_domain(blog) if blog and "." in blog else f"{org}.com"
                return {"name": name, "domain": domain, "country": "US", "source": "github"}
            except Exception:
                return None

    results = await asyncio.gather(*[_one(o) for o in _GITHUB_ORGS])
    companies = [r for r in results if r]
    logger.info("[DISCOVERY] github=%d", len(companies))
    return companies


def _dedup(candidates: list[dict]) -> list[dict]:
    seen_d: set[str] = set()
    seen_n: set[str] = set()
    out: list[dict] = []
    for c in candidates:
        d = c.get("domain", "").lower().strip()
        n = c.get("name", "").lower().strip()
        if not n or not d or "." not in d:
            continue
        if d in seen_d or n in seen_n:
            continue
        seen_d.add(d)
        seen_n.add(n)
        out.append(c)
    return out


async def _upsert_companies(
    db: AsyncSession,
    companies: list[dict],
    ats_map: dict[str, ATSResult],
) -> tuple[int, int]:
    inserted = skipped = 0
    for c in companies:
        ats = ats_map.get(c["domain"])
        if ats is None:
            skipped += 1
            continue
        stmt = pg_insert(Company).values(
            name                   = c["name"][:500],
            domain                 = c["domain"][:255],
            ats_type               = ats.provider,
            ats_identifier         = ats.slug,
            country                = c.get("country", "US"),
            active                 = True,
            priority_score         = 65 if ats.job_count > 0 else 40,
            scan_frequency_minutes = 360,
        ).on_conflict_do_update(
            index_elements=["domain"],
            set_={"ats_type": ats.provider, "ats_identifier": ats.slug, "active": True},
        )
        try:
            await db.execute(stmt)
            inserted += 1
        except Exception as exc:
            logger.debug("[DISCOVERY] upsert_skip name=%s err=%s", c["name"], exc)
            skipped += 1
    await db.commit()
    return inserted, skipped


async def run_company_discovery() -> dict:
    """Main discovery entry point — called by scheduler every 6 hours."""
    logger.info("[DISCOVERY] start")
    t0 = time.monotonic()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        yc     = await _fetch_yc_companies(client)
        github = await _fetch_github_orgs(client)

    curated = [
        {"name": n, "domain": d, "country": c, "source": "curated"}
        for n, d, c in _CURATED
    ]

    all_candidates = curated + yc + github
    deduped        = _dedup(all_candidates)[: _MAX_PER_RUN]
    logger.info("[DISCOVERY] candidates=%d deduped=%d", len(all_candidates), len(deduped))

    # Filter out already-known domains
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Company.domain).where(Company.domain.isnot(None)))).all()
        known_domains = {r[0] for r in rows}

    new_cos = [c for c in deduped if c["domain"] not in known_domains]
    logger.info("[DISCOVERY] new=%d (known=%d)", len(new_cos), len(known_domains))

    if not new_cos:
        elapsed = round(time.monotonic() - t0, 1)
        return {"inserted": 0, "skipped": 0, "runtime_s": elapsed}

    # Probe ATS concurrently
    sem = asyncio.Semaphore(_CONCURRENCY)
    ats_map: dict[str, ATSResult] = {}

    async def _probe(c: dict) -> None:
        r = await detect_ats(c["name"], c["domain"], semaphore=sem)
        if r:
            ats_map[c["domain"]] = r

    await asyncio.gather(*[_probe(c) for c in new_cos])
    logger.info("[DISCOVERY] ats_detected=%d/%d", len(ats_map), len(new_cos))

    async with AsyncSessionLocal() as db:
        inserted, skipped = await _upsert_companies(db, new_cos, ats_map)

    elapsed = round(time.monotonic() - t0, 1)
    logger.info(
        "[DISCOVERY] done inserted=%d skipped=%d runtime_s=%s",
        inserted, skipped, elapsed,
    )
    return {"inserted": inserted, "skipped": skipped, "runtime_s": elapsed}
