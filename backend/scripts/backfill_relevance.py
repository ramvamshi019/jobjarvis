#!/usr/bin/env python3
"""
One-time backfill so the relevance/freshness fixes apply to the EXISTING
corpus (the code changes are otherwise forward-only).

For every active job it:
  1. Re-runs classify_role()  -> fixes the "Software Engineer -> Other" bug
     and every other under-tagged role_category retroactively.
  2. Recomputes freshness_label from the real posted_at (falls back to
     first_seen_at) -> fixes the "30-day-old job shows 19h ago" lie.
  3. Soft-deactivates clearly non-tech rows (cook/nurse/cashier/...) using
     the same conservative gate as ingestion. active=False only — fully
     reversible, never deletes.

Safe by design: idempotent (only writes changed fields), batched by id
(keyset, ~constant memory), and DRY-RUN unless you pass --apply.

Usage (run inside the worker/backend container, AFTER a fresh DB backup):

  # dry run — shows what WOULD change, writes nothing
  docker compose exec backend python -m scripts.backfill_relevance

  # apply for real
  docker compose exec backend python -m scripts.backfill_relevance --apply

  # apply but keep non-tech rows active (only re-tag role + freshness)
  docker compose exec backend python -m scripts.backfill_relevance --apply --no-deactivate
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.job import Job
from app.ai.role_classifier import classify_role
from app.services.freshness import compute_freshness
from app.workers.scan_tasks import _is_tech_title


async def run(apply: bool, deactivate: bool, batch: int, limit: int | None) -> None:
    scanned = role_changed = fresh_changed = deactivated = 0
    last_id = 0

    while True:
        async with AsyncSessionLocal() as db:
            q = (
                select(Job)
                .where(Job.active == True, Job.id > last_id)  # noqa: E712
                .order_by(Job.id)
                .limit(batch)
            )
            jobs = list((await db.execute(q)).scalars().all())
            if not jobs:
                break

            for job in jobs:
                scanned += 1
                last_id = job.id

                # 3. Non-tech soft-deactivate (do this first; if it's gone
                #    we don't care about its role/freshness).
                if deactivate and not _is_tech_title(job.title or ""):
                    job.active = False
                    deactivated += 1
                    continue

                # 1. Role re-classification.
                new_role = classify_role(
                    job.title or "", job.description or ""
                ).role_category
                if new_role and new_role != job.role_category:
                    job.role_category = new_role
                    role_changed += 1

                # 2. Freshness from the real posting date.
                new_fresh = compute_freshness(job.posted_at or job.first_seen_at)
                if new_fresh and new_fresh != job.freshness_label:
                    job.freshness_label = new_fresh
                    fresh_changed += 1

            if apply:
                await db.commit()
            else:
                await db.rollback()

        print(
            f"  …{scanned:>7} scanned | role+{role_changed} "
            f"fresh+{fresh_changed} deactivated+{deactivated} "
            f"(last_id={last_id})",
            flush=True,
        )

        if limit and scanned >= limit:
            break

    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    print(
        f"\n{mode}\n"
        f"  scanned          : {scanned}\n"
        f"  role re-tagged   : {role_changed}\n"
        f"  freshness fixed  : {fresh_changed}\n"
        f"  non-tech disabled: {deactivated}",
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill role/freshness/relevance.")
    ap.add_argument("--apply", action="store_true",
                    help="Write changes (default: dry-run, no writes).")
    ap.add_argument("--no-deactivate", action="store_true",
                    help="Do not soft-deactivate non-tech rows.")
    ap.add_argument("--batch", type=int, default=2000,
                    help="Rows per batch / commit (default 2000).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N rows (for testing).")
    args = ap.parse_args()

    if not args.apply:
        print("DRY-RUN: nothing will be written. Re-run with --apply.\n")

    asyncio.run(
        run(
            apply=args.apply,
            deactivate=not args.no_deactivate,
            batch=args.batch,
            limit=args.limit,
        )
    )


if __name__ == "__main__":
    main()
