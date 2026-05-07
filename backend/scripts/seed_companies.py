#!/usr/bin/env python3
"""Seed the database with companies from CSV."""
import asyncio
import csv
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.company import Company


async def seed():
    csv_path = Path(__file__).parent.parent / "data" / "companies_seed.csv"
    created = 0
    skipped = 0

    async with AsyncSessionLocal() as db:
        seen_domains = set()
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name", "").strip()
                domain = (row.get("domain", "").strip() or "").lower() or None
                if not name:
                    continue

                if domain:
                    if domain in seen_domains:
                        skipped += 1
                        continue
                    
                    existing = await db.execute(select(Company.id).where(Company.domain == domain))
                    if existing.scalar_one_or_none():
                        skipped += 1
                        seen_domains.add(domain)
                        continue
                    
                    seen_domains.add(domain)

                priority = int(row.get("priority_score", 50) or 50)
                freq = int(row.get("scan_frequency_minutes", 360) or 360)

                company = Company(
                    name=name,
                    domain=domain,
                    career_url=row.get("career_url", "").strip() or None,
                    ats_type=row.get("ats_type", "").strip() or None,
                    ats_identifier=row.get("ats_identifier", "").strip() or None,
                    country=row.get("country", "US").strip() or "US",
                    priority_score=priority,
                    scan_frequency_minutes=freq,
                    next_scan_at=datetime.now(timezone.utc) + timedelta(seconds=10),
                    active=True,
                )
                db.add(company)
                created += 1
                
                # Periodically flush to keep memory low and catch errors early
                if created % 100 == 0:
                    await db.flush()

        await db.commit()

    print(f"✅ Seeded {created} companies, skipped {skipped} duplicates")


if __name__ == "__main__":
    asyncio.run(seed())
