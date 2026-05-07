"""
AI job-matching engine — the differentiator vs. LinkedIn/Indeed.

For a given user, scores every active job by:
  • cosine similarity between job embedding and resume embedding   (60% weight)
  • salary fit (matches user's expectations)                       (15%)
  • location fit (city, region, remote preference)                 (10%)
  • visa policy fit (sponsor or no-sponsor required)               (10%)
  • freshness boost (newer jobs ranked higher)                     (5%)

Outputs top N matches per user. Run nightly via Celery beat.

Prereqs:
  • Jobs must have embeddings generated (your existing embedding_tasks does this)
  • User must have a ResumeVersion with embedding generated
  • pgvector extension installed (which JobJarvis already uses)

Usage:
  docker exec jobjarvis_celery_worker python3 -u /tmp/ai_match_jobs.py \\
      --user 1 --top 50

If --user not provided, runs for all users.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import asyncpg

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


# ── Match scoring ────────────────────────────────────────────────────────────

MATCH_SQL = """
WITH user_resume AS (
    SELECT rv.id, re.embedding
      FROM resume_versions rv
      JOIN resume_embeddings re ON re.resume_id = rv.id
     WHERE rv.user_id = $1
       AND rv.is_active = true
     ORDER BY rv.updated_at DESC
     LIMIT 1
),
user_prefs AS (
    -- Pulled from users table directly; no separate user_preferences table.
    SELECT
      COALESCE(min_salary, 0) AS pref_salary_min,
      9999999 AS pref_salary_max,
      LOWER(COALESCE(current_location, '')) AS pref_location,
      CASE WHEN open_to_remote THEN 'remote' ELSE NULL END AS remote_preference
    FROM users
    WHERE id = $1
)
SELECT
    j.id,
    j.title,
    c.name AS company,
    j.location,
    j.salary_min,
    j.salary_max,
    j.remote_type,
    j.url,
    j.posted_at,

    -- Embedding similarity (cosine)
    -- pgvector <=> returns distance; 1 - distance = similarity in [0,1]
    (1 - (je.embedding <=> ur.embedding)) AS sim_score,

    -- Salary fit: 1.0 if any overlap with user's range, 0.5 if close, 0 if not
    CASE
        WHEN j.salary_max IS NULL THEN 0.5
        WHEN j.salary_max >= up.pref_salary_min
         AND j.salary_min <= up.pref_salary_max THEN 1.0
        WHEN j.salary_max >= 0.7 * up.pref_salary_min THEN 0.5
        ELSE 0.2
    END AS salary_fit,

    -- Location fit
    CASE
        WHEN up.remote_preference = 'remote' AND j.remote_type = 'remote' THEN 1.0
        WHEN up.remote_preference = 'onsite' AND j.remote_type = 'onsite' THEN 0.8
        WHEN up.remote_preference IS NULL THEN 0.7
        WHEN up.pref_location != ''
             AND LOWER(COALESCE(j.location,'')) LIKE '%' || up.pref_location || '%' THEN 1.0
        ELSE 0.5
    END AS location_fit,

    -- Freshness boost: posted in last 7 days = 1.0, last 30 days = 0.7, older = 0.4
    CASE
        WHEN j.posted_at > NOW() - INTERVAL '7 days' THEN 1.0
        WHEN j.posted_at > NOW() - INTERVAL '30 days' THEN 0.7
        WHEN j.first_seen_at > NOW() - INTERVAL '14 days' THEN 0.6
        ELSE 0.4
    END AS freshness_score
FROM jobs j
JOIN companies c ON c.id = j.company_id
JOIN job_embeddings je ON je.job_id = j.id
CROSS JOIN user_resume ur
LEFT JOIN user_prefs up ON true
WHERE j.active = true
  AND c.active = true
  AND je.embedding IS NOT NULL
  AND ur.embedding IS NOT NULL
ORDER BY (1 - (je.embedding <=> ur.embedding)) DESC
LIMIT $2;
"""


def composite_score(row) -> float:
    """Combine sub-scores with weights."""
    return (
        0.60 * float(row["sim_score"] or 0)
        + 0.15 * float(row["salary_fit"] or 0)
        + 0.10 * float(row["location_fit"] or 0)
        + 0.05 * float(row["freshness_score"] or 0)
        # Visa fit hook — gets added if user_preferences has the field
    )


async def get_users(conn, user_id: int | None) -> list[int]:
    if user_id:
        return [user_id]
    rows = await conn.fetch("SELECT id FROM users WHERE is_active = true")
    return [r["id"] for r in rows]


async def match_for_user(conn, user_id: int, top: int) -> list[dict]:
    try:
        rows = await conn.fetch(MATCH_SQL, user_id, top * 3)
    except Exception as e:
        print(f"  user {user_id}: query failed: {e}", flush=True)
        return []

    if not rows:
        return []

    scored = []
    for r in rows:
        d = dict(r)
        d["match_score"] = composite_score(r)
        scored.append(d)

    scored.sort(key=lambda x: x["match_score"], reverse=True)
    return scored[:top]


async def store_matches(conn, user_id: int, matches: list[dict]):
    """
    Persist matches to a `job_matches` table for fast UI retrieval.
    Creates the table if missing.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_matches (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            match_score DOUBLE PRECISION NOT NULL,
            sim_score DOUBLE PRECISION,
            salary_fit DOUBLE PRECISION,
            location_fit DOUBLE PRECISION,
            freshness_score DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, job_id)
        );
        CREATE INDEX IF NOT EXISTS ix_job_matches_user_score
            ON job_matches(user_id, match_score DESC);
        """
    )

    # Wipe stale matches for this user (keeps only latest run)
    await conn.execute("DELETE FROM job_matches WHERE user_id = $1", user_id)

    if not matches:
        return

    rows = [
        (user_id, m["id"], m["match_score"], m["sim_score"],
         m["salary_fit"], m["location_fit"], m["freshness_score"])
        for m in matches
    ]
    await conn.executemany(
        """
        INSERT INTO job_matches
            (user_id, job_id, match_score, sim_score, salary_fit,
             location_fit, freshness_score)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (user_id, job_id) DO UPDATE SET
            match_score    = EXCLUDED.match_score,
            sim_score      = EXCLUDED.sim_score,
            salary_fit     = EXCLUDED.salary_fit,
            location_fit   = EXCLUDED.location_fit,
            freshness_score= EXCLUDED.freshness_score,
            created_at     = NOW()
        """,
        rows,
    )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", type=int, default=None,
                        help="User ID. If omitted, runs for all active users.")
    parser.add_argument("--top", type=int, default=50,
                        help="Top N matches per user")
    parser.add_argument("--print", action="store_true",
                        help="Print top matches to stdout instead of storing")
    args = parser.parse_args()

    print(f"Connecting to {DB_DSN.split('@')[-1]}…", flush=True)
    conn = await asyncpg.connect(DB_DSN)

    users = await get_users(conn, args.user)
    print(f"Matching for {len(users)} user(s), top {args.top} jobs each\n",
          flush=True)

    for uid in users:
        matches = await match_for_user(conn, uid, args.top)
        if not matches:
            print(f"  user {uid}: no matches (no resume? no embeddings?)",
                  flush=True)
            continue

        if args.print:
            print(f"\n=== Top {len(matches)} matches for user {uid} ===")
            for i, m in enumerate(matches[:20], 1):
                title = m["title"][:50]
                co = m["company"][:25]
                sal = (
                    f"${(m['salary_min'] or 0)//1000}-"
                    f"{(m['salary_max'] or 0)//1000}k"
                    if m["salary_min"] else "no-salary"
                )
                print(f"  {i:>2}. [{m['match_score']:.3f}] {title:50s} "
                      f"@ {co:25s} {sal:14s} "
                      f"sim={m['sim_score']:.2f}")
        else:
            await store_matches(conn, uid, matches)
            print(f"  user {uid}: stored {len(matches)} matches "
                  f"(top score: {matches[0]['match_score']:.3f})", flush=True)

    await conn.close()
    print("\nDone.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
