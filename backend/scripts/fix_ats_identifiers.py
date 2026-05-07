#!/usr/bin/env python3
"""
Fix stale ATS identifiers.
Many companies have moved from Lever to Greenhouse or other systems.
Run: docker compose exec backend python scripts/fix_ats_identifiers.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import update
from app.database import AsyncSessionLocal
from app.models.company import Company


# Companies that moved from Lever → Greenhouse (verified board slugs)
LEVER_TO_GREENHOUSE = [
    # (domain, greenhouse_slug)
    ("amplitude.com",    "amplitude"),
    ("pagerduty.com",    "pagerduty"),
    ("mixpanel.com",     "mixpanel"),
    ("hashicorp.com",    "hashicorp"),
    ("fivetran.com",     "fivetran"),
    ("grafana.com",      "grafana-labs"),
    ("wandb.ai",         "wandb"),
    ("deepgram.com",     "deepgram"),
    ("elevenlabs.io",    "elevenlabs"),
    ("astronomer.io",    "astronomer"),
    ("getdbt.com",       "dbt-labs"),    # dbt Labs (both rows share domain; seeds deduplicates)
    ("twilio.com",       "twilio"),
    ("block.xyz",        "block"),       # Square/Block
]

# Companies that moved from Lever → Workday
LEVER_TO_WORKDAY = [
    # (domain, workday_id)  workday_id = "<slug>|<TenantName>"
    ("segment.com",      None),   # absorbed into Twilio — disable instead
]

# Companies to DISABLE: open-source projects, dissolved, custom portals we can't parse
DISABLE_DOMAINS = [
    "airflow.apache.org",   # Apache open-source project, not a company
    "segment.com",          # Dissolved into Twilio
    "preset.io",            # Acquired by Salesforce, no longer hiring separately
    "meta.com",             # Custom metacareers portal — not Lever
    "x.com",                # Twitter/X closed their Lever board
    "nuro.ai",              # Acquired / wound down
    "stability.ai",         # Company in turmoil, Lever board closed
    "inflection.ai",        # Most team moved to Microsoft, board closed
    "adept.ai",             # Acquired by Salesforce, board closed
    "imbue.com",            # Renamed/restructured, board closed
    "openai.com",           # Moved to Ashby (no connector yet)
    "airbyte.com",          # Moved to Ashby (no connector yet)
    "llamaindex.ai",        # Moved to Ashby (no connector yet)
    "trychroma.com",        # Moved to Ashby (no connector yet)
    "suno.com",             # Moved to Ashby (no connector yet)
    "gretel.ai",            # Moved to Ashby (no connector yet)
    "deepmind.google",      # DeepMind uses internal Google hiring, not Lever
    "runwayml.com",         # Moved to Greenhouse (check) or Ashby
    "together.ai",          # Moved to Greenhouse or Ashby
    "bentoml.com",          # Small, likely Ashby
    "character.ai",         # Moved to Greenhouse
]

# Companies with WRONG Lever identifier (identifier typo / slug changed)
LEVER_IDENTIFIER_FIXES = [
    # (domain, correct_lever_slug)
    ("cohere.com",       "cohere"),          # was "cohere-ai"
    ("mistral.ai",       "mistral"),         # was "mistral-ai"
    ("scale.com",        "scaleai"),         # was "scale-ai"
]


async def fix():
    total_greenhouse = 0
    total_disabled = 0
    total_lever_fixed = 0

    async with AsyncSessionLocal() as db:

        # 1. Lever → Greenhouse
        for domain, slug in LEVER_TO_GREENHOUSE:
            result = await db.execute(
                update(Company)
                .where(Company.domain == domain)
                .values(ats_type="greenhouse", ats_identifier=slug, next_scan_at=None)
                .returning(Company.name)
            )
            rows = result.fetchall()
            for (name,) in rows:
                print(f"  ✅ {name} ({domain})  lever → greenhouse/{slug}")
                total_greenhouse += 1

        # 2. Disable
        for domain in DISABLE_DOMAINS:
            result = await db.execute(
                update(Company)
                .where(Company.domain == domain)
                .values(active=False)
                .returning(Company.name)
            )
            rows = result.fetchall()
            for (name,) in rows:
                print(f"  🚫 {name} ({domain})  → disabled")
                total_disabled += 1

        # 3. Fix wrong Lever identifiers
        for domain, correct_slug in LEVER_IDENTIFIER_FIXES:
            result = await db.execute(
                update(Company)
                .where(Company.domain == domain)
                .values(ats_identifier=correct_slug, next_scan_at=None)
                .returning(Company.name)
            )
            rows = result.fetchall()
            for (name,) in rows:
                print(f"  🔧 {name} ({domain})  lever/{correct_slug} (fixed slug)")
                total_lever_fixed += 1

        await db.commit()

    print(f"\n✅ Done: {total_greenhouse} moved to Greenhouse, {total_disabled} disabled, {total_lever_fixed} lever slugs fixed")


if __name__ == "__main__":
    asyncio.run(fix())
