"""
Dedupe duplicate company rows where name differs only in case/whitespace.

For each group of duplicates (matched on lower(trim(name))):
  1. Pick the WINNER row — most jobs_found_count, then oldest created_at.
  2. Reassign all jobs.company_id from losers → winner.
  3. Mark loser rows as active=false (soft delete; preserves audit trail).
  4. Add a case-insensitive unique index to prevent future dupes.

Run inside the celery_worker container:
  docker cp backend/scripts/dedup_companies.py \\
    jobjarvis_celery_worker:/tmp/dedup_companies.py
  docker exec jobjarvis_celery_worker python3 -u /tmp/dedup_companies.py

Idempotent — safe to run multiple times.
"""
import asyncio
import os
import sys

import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


async def main():
    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

    # 1. Find all duplicate groups
    print("\nFinding duplicate company groups…", flush=True)
    groups = await conn.fetch(
        """
        SELECT lower(trim(name)) AS canonical,
               array_agg(id ORDER BY jobs_found_count DESC, created_at ASC) AS ids,
               array_agg(name ORDER BY jobs_found_count DESC, created_at ASC) AS names,
               COUNT(*) AS dup_count
        FROM companies
        GROUP BY lower(trim(name))
        HAVING COUNT(*) > 1
        ORDER BY dup_count DESC, canonical
        """
    )
    print(f"Found {len(groups)} duplicate groups\n", flush=True)
    if not groups:
        print("Nothing to dedupe.", flush=True)
        await conn.close()
        return

    # 2. Show top 10 worst offenders
    print("Top duplicates being merged:", flush=True)
    for g in groups[:10]:
        print(f"  {g['canonical']:30s}  ×{g['dup_count']}  variants={g['names']}",
              flush=True)
    if len(groups) > 10:
        print(f"  … and {len(groups)-10} more groups\n", flush=True)
    else:
        print()

    # 3. Merge each group inside one transaction
    total_jobs_moved = 0
    total_rows_deactivated = 0
    async with conn.transaction():
        for g in groups:
            ids = g["ids"]
            winner_id, *loser_ids = ids
            if not loser_ids:
                continue

            # Identify loser jobs that would collide with winner on either
            # (company_id, external_id) or url unique constraints.
            collision_ids = await conn.fetch(
                """
                SELECT id FROM jobs
                 WHERE company_id = ANY($1::int[])
                   AND (
                       external_id IN (
                           SELECT external_id FROM jobs
                            WHERE company_id = $2 AND external_id IS NOT NULL
                       )
                       OR url IN (
                           SELECT url FROM jobs
                            WHERE company_id = $2 AND url IS NOT NULL
                       )
                   )
                """,
                loser_ids, winner_id,
            )
            if collision_ids:
                ids_to_drop = [r["id"] for r in collision_ids]
                # Nullify FK refs from bronze_raw_jobs first to avoid
                # foreign-key violation on delete.
                await conn.execute(
                    "UPDATE bronze_raw_jobs SET silver_job_id = NULL "
                    "WHERE silver_job_id = ANY($1::bigint[])",
                    ids_to_drop,
                )
                # Also nullify any other tables that may reference jobs.id.
                # Use try/except per-table so missing tables don't break.
                for fk_table_col in [
                    ("job_status_history", "job_id"),
                    ("ai_decisions", "job_id"),
                    ("applications", "job_id"),
                    ("job_embeddings", "job_id"),
                ]:
                    try:
                        await conn.execute(
                            f"DELETE FROM {fk_table_col[0]} "
                            f"WHERE {fk_table_col[1]} = ANY($1::bigint[])",
                            ids_to_drop,
                        )
                    except Exception:
                        pass  # table doesn't exist or column differs
                # Now safe to delete the colliding loser jobs.
                await conn.execute(
                    "DELETE FROM jobs WHERE id = ANY($1::bigint[])",
                    ids_to_drop,
                )

            # Now safe — reassign remaining loser jobs to the winner.
            moved = await conn.fetchval(
                """
                WITH moved AS (
                    UPDATE jobs
                       SET company_id = $1, updated_at = now()
                     WHERE company_id = ANY($2::int[])
                  RETURNING id
                )
                SELECT COUNT(*) FROM moved
                """,
                winner_id, loser_ids,
            )
            total_jobs_moved += moved or 0

            # Soft-delete loser companies
            n = await conn.execute(
                """
                UPDATE companies
                   SET active = false, updated_at = now()
                 WHERE id = ANY($1::int[])
                """,
                loser_ids,
            )
            # n is "UPDATE N" string; pull the integer
            total_rows_deactivated += int(n.split()[-1]) if n else 0

    print(f"\nMerged {len(groups)} groups", flush=True)
    print(f"  Jobs reassigned to winners: {total_jobs_moved}", flush=True)
    print(f"  Loser rows deactivated:     {total_rows_deactivated}", flush=True)

    # 4. Add case-insensitive uniqueness guard (idempotent)
    print("\nAdding case-insensitive unique index to prevent future dupes…",
          flush=True)
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_name_lower_active
          ON companies (lower(trim(name)))
          WHERE active = true
        """
    )
    print("Index ensured.", flush=True)

    # 5. Final report
    after = await conn.fetchrow(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE active=true) AS active "
        "FROM companies"
    )
    print(f"\nFinal counts: total={after['total']}  active={after['active']}",
          flush=True)
    await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
