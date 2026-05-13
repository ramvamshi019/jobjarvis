"""
Match service — cosine similarity between user's active resume embedding
and every job embedding, with composite scoring (similarity + salary fit +
location fit + freshness).

Mirrors backend/scripts/ai_match_jobs.py but runs inside the FastAPI app
using the existing AsyncSession.
"""
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# Same query the script uses, but referencing the User model fields directly
# (no separate user_preferences table).
_MATCH_SQL = text(
    """
    WITH user_resume AS (
        SELECT rv.id AS resume_id, re.embedding
          FROM resume_versions rv
          JOIN resume_embeddings re ON re.resume_id = rv.id
         WHERE rv.user_id = :user_id
           AND rv.is_active = true
         ORDER BY rv.updated_at DESC
         LIMIT 1
    ),
    user_prefs AS (
        SELECT
          COALESCE(min_salary, 0)                AS pref_salary_min,
          9999999                                AS pref_salary_max,
          LOWER(COALESCE(current_location, '')) AS pref_location,
          CASE WHEN open_to_remote THEN 'remote' ELSE NULL END AS remote_preference
        FROM users
        WHERE id = :user_id
    )
    SELECT
        j.id, j.title, j.company_name, j.location, j.remote_type,
        j.salary_min, j.salary_max, j.url, j.apply_url,
        j.posted_at, j.first_seen_at,
        c.name AS company, c.ats AS source_ats,

        (1 - (je.embedding <=> ur.embedding)) AS sim_score,

        CASE
            WHEN j.salary_max IS NULL THEN 0.5
            WHEN j.salary_max >= up.pref_salary_min
             AND (j.salary_min IS NULL OR j.salary_min <= up.pref_salary_max) THEN 1.0
            WHEN j.salary_max >= 0.7 * up.pref_salary_min THEN 0.5
            ELSE 0.2
        END AS salary_fit,

        CASE
            WHEN up.remote_preference = 'remote' AND j.remote_type = 'remote' THEN 1.0
            WHEN up.remote_preference IS NULL THEN 0.7
            WHEN up.pref_location <> ''
                 AND LOWER(COALESCE(j.location,'')) LIKE '%' || up.pref_location || '%' THEN 1.0
            ELSE 0.5
        END AS location_fit,

        CASE
            WHEN j.posted_at > NOW() - INTERVAL '7 days'  THEN 1.0
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
      -- US-friendly default: US + Remote roles, plus uncategorized
      -- (uncategorized often includes US jobs whose location string didn't
      -- match the country backfill regex; we'd rather include than exclude).
      AND (j.country IN ('US', 'REMOTE') OR j.country IS NULL)
      -- Hard-exclude obvious non-US/non-remote countries
      AND (j.country IS NULL OR j.country NOT IN
           ('IN','GB','DE','FR','ES','NL','BR','AU','SG','JP','IE','MX'))
    ORDER BY (1 - (je.embedding <=> ur.embedding)) DESC
    LIMIT :limit
    """
)


# Split into two separate text() calls — SQLAlchemy text() doesn't reliably
# run multi-statement DDL.
_ENSURE_TABLE_SQL = text(
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
    )
    """
)

_ENSURE_INDEX_SQL = text(
    """
    CREATE INDEX IF NOT EXISTS ix_job_matches_user_score
        ON job_matches(user_id, match_score DESC)
    """
)


def _composite(row) -> float:
    """0.60 * sim + 0.15 * salary + 0.10 * location + 0.05 * freshness."""
    return (
        0.60 * float(row["sim_score"] or 0)
        + 0.15 * float(row["salary_fit"] or 0)
        + 0.10 * float(row["location_fit"] or 0)
        + 0.05 * float(row["freshness_score"] or 0)
    )


async def recompute_matches_for_user(
    db: AsyncSession, user_id: int, top: int = 50,
) -> int:
    """
    Run the cosine + composite scoring against all active embedded jobs and
    persist the top-N to job_matches. Returns the number persisted.
    """
    # Ensure table + index exist (idempotent)
    await db.execute(_ENSURE_TABLE_SQL)
    await db.execute(_ENSURE_INDEX_SQL)
    await db.commit()

    # Pull a wider pool than `top` to allow re-ranking by composite score
    pool = max(top * 3, 100)
    res = await db.execute(_MATCH_SQL, {"user_id": user_id, "limit": pool})
    rows = res.mappings().all()
    if not rows:
        return 0

    scored = []
    for r in rows:
        d = dict(r)
        d["match_score"] = _composite(r)
        scored.append(d)
    scored.sort(key=lambda x: x["match_score"], reverse=True)
    matches = scored[:top]

    # Wipe stale matches for this user
    await db.execute(
        text("DELETE FROM job_matches WHERE user_id = :uid"),
        {"uid": user_id},
    )

    # Insert fresh matches
    insert_sql = text(
        """
        INSERT INTO job_matches
            (user_id, job_id, match_score, sim_score, salary_fit,
             location_fit, freshness_score)
        VALUES
            (:user_id, :job_id, :match_score, :sim_score, :salary_fit,
             :location_fit, :freshness_score)
        """
    )
    for m in matches:
        await db.execute(insert_sql, {
            "user_id": user_id,
            "job_id": m["id"],
            "match_score": m["match_score"],
            "sim_score": m["sim_score"],
            "salary_fit": m["salary_fit"],
            "location_fit": m["location_fit"],
            "freshness_score": m["freshness_score"],
        })
    await db.commit()
    return len(matches)


async def list_matches_for_user(
    db: AsyncSession, user_id: int, limit: int = 50,
    country: str = "us",          # "us" | "remote" | "all"
    recency_days: int | None = None,  # None = no recency filter
) -> list[dict]:
    """
    Read the persisted job_matches for a user, joined with the jobs table
    so the API can return rich rows for the UI.

    Args:
      country: "us" → US + Remote + uncategorized (excluding obvious non-US);
               "remote" → Remote-only;
               "all" → no country filter.
      recency_days: limit to jobs first seen in the last N days.
    """
    where_clauses = [
        "jm.user_id = :uid",
        "j.active = true",
        "c.active = true",
    ]
    params: dict = {"uid": user_id, "limit": limit}

    # Country filter
    if country == "us":
        where_clauses.append(
            "(j.country IN ('US', 'REMOTE') OR j.country IS NULL)"
        )
        where_clauses.append(
            "(j.country IS NULL OR j.country NOT IN "
            "('IN','GB','DE','FR','ES','NL','BR','AU','SG','JP','IE','MX'))"
        )
        # Belt-and-suspenders: exclude obvious non-US location text
        where_clauses.append(
            "NOT (LOWER(COALESCE(j.location,'')) ~ "
            "'\\\\m(emea|apac|latam|asia|europe|"
            "barcelona|madrid|berlin|munich|amsterdam|paris|london|dublin|"
            "armenia|cyprus|warsaw|prague|bucharest|kyiv|"
            "bangalore|hyderabad|delhi|mumbai|chennai|pune|"
            "tokyo|osaka|seoul|singapore|sydney|melbourne|sao paulo|"
            "mexico city|guadalajara|toronto|vancouver)\\\\M')"
        )
    elif country == "remote":
        where_clauses.append("j.country = 'REMOTE' OR j.remote_type = 'remote'")

    # Recency filter — use posted_at if available, else fall back to first_seen_at
    if recency_days and recency_days > 0:
        where_clauses.append(
            "COALESCE(j.posted_at, j.first_seen_at) > NOW() - INTERVAL '{} days'"
            .format(int(recency_days))
        )

    sql = text(
        f"""
        SELECT
            jm.match_score, jm.sim_score, jm.salary_fit,
            jm.location_fit, jm.freshness_score, jm.created_at AS matched_at,
            j.id, j.title, j.company_name, j.location, j.remote_type,
            j.salary_min, j.salary_max, j.url, j.apply_url,
            j.posted_at, j.first_seen_at,
            c.ats AS source_ats
        FROM job_matches jm
        JOIN jobs j      ON j.id  = jm.job_id
        JOIN companies c ON c.id  = j.company_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY jm.match_score DESC
        LIMIT :limit
        """
    )
    res = await db.execute(sql, params)
    return [dict(r) for r in res.mappings().all()]
