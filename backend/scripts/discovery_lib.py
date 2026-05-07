"""
Shared helpers for company-discovery scripts.

  - detect_ats(url) → (ats_type, slug) | None
        Classifies a careers/jobs URL into one of our 10 known ATS types
        and pulls out the company slug.

  - upsert_company(conn, name, ats, slug, careers_url) → int | None
        Idempotent insert into the `companies` table with all NOT NULL
        defaults. Respects the case-insensitive unique index added by
        dedup_companies.py.

  - get_db_conn() → asyncpg connection
"""
import os
import re
from datetime import datetime, timezone, timedelta

import asyncpg


DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobjarvis:jobjarvis@postgres:5432/jobjarvis",
).replace("postgresql+asyncpg://", "postgresql://")


# ── ATS URL classifier ──────────────────────────────────────────────────────
# Order matters: more specific patterns first.  Each returns the slug as
# group(1).  Patterns are case-insensitive.

_ATS_PATTERNS = [
    # Greenhouse — both legacy and new domains
    (re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9][\w-]+)", re.I), "greenhouse"),
    (re.compile(r"greenhouse\.io/([a-z0-9][\w-]+)", re.I),                                                 "greenhouse"),

    # Lever
    (re.compile(r"jobs\.lever\.co/([a-z0-9][\w-]+)", re.I), "lever"),

    # Ashby
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][\w-]+)", re.I), "ashby"),

    # SmartRecruiters
    (re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9][\w-]+)", re.I), "smartrecruiters"),

    # Workable
    (re.compile(r"apply\.workable\.com/([a-z0-9][\w-]+)", re.I), "workable"),
    (re.compile(r"([a-z0-9][\w-]+)\.workable\.com",       re.I), "workable"),

    # BambooHR
    (re.compile(r"([a-z0-9][\w-]+)\.bamboohr\.com",       re.I), "bamboohr"),

    # Recruitee
    (re.compile(r"([a-z0-9][\w-]+)\.recruitee\.com",      re.I), "recruitee"),

    # iCIMS
    (re.compile(r"careers-([a-z0-9][\w-]+)\.icims\.com",  re.I), "icims"),
    (re.compile(r"([a-z0-9][\w-]+)\.icims\.com",          re.I), "icims"),

    # Workday — many F500 use this
    (re.compile(r"([a-z0-9][\w-]+)\.(?:wd[0-9]+\.)?myworkdayjobs\.com", re.I), "workday"),

    # TeamTailor
    (re.compile(r"([a-z0-9][\w-]+)\.teamtailor\.com",     re.I), "teamtailor"),

    # Personio (EU SMB)
    (re.compile(r"([a-z0-9][\w-]+)\.personio\.de",        re.I), "personio"),
    (re.compile(r"([a-z0-9][\w-]+)\.jobs\.personio\.de",  re.I), "personio"),

    # Jobvite
    (re.compile(r"jobs\.jobvite\.com/([a-z0-9][\w-]+)",   re.I), "jobvite"),

    # YC company pages
    (re.compile(r"ycombinator\.com/companies/([a-z0-9][\w-]+)", re.I), "yc"),
]


def detect_ats(url: str) -> tuple[str, str] | None:
    """Classify a URL → (ats_type, slug) or None if unknown."""
    if not url:
        return None
    for pat, ats_type in _ATS_PATTERNS:
        m = pat.search(url)
        if m:
            slug = m.group(1).strip().lower()
            # Filter junk slugs
            if slug in {"www", "jobs", "careers", "support", "embed",
                        "static", "api", "help", "privacy"}:
                return None
            if len(slug) < 2 or len(slug) > 80:
                return None
            return (ats_type, slug)
    return None


# ── Company name guesser ─────────────────────────────────────────────────────

def slug_to_name(slug: str) -> str:
    """Convert a slug like 'acme-corp' → 'Acme Corp' for display."""
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", slug) if w)


# ── Postgres helpers ─────────────────────────────────────────────────────────

async def get_db_conn() -> asyncpg.Connection:
    return await asyncpg.connect(DB_DSN)


async def upsert_company(
    conn: asyncpg.Connection,
    name: str,
    ats: str,
    slug: str,
    careers_url: str,
) -> int | None:
    """
    Idempotent upsert into companies. Returns the row id, or None on failure.

    Uses ON CONFLICT (name) — relies on the case-insensitive unique index that
    dedup_companies.py installed.  If a row with the same lower(trim(name))
    already exists, we leave it alone.
    """
    if not name or not slug:
        return None
    name = name[:500].strip()
    slug = slug[:500].strip()
    careers_url = (careers_url or "")[:5000]

    now = datetime.now(timezone.utc)
    next_scan = now + timedelta(minutes=30)

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO companies (
                name, ats, ats_identifier, careers_url,
                priority_score, scan_priority,
                scan_frequency_minutes, next_scan_at, active,
                failure_count, consecutive_failures, jobs_found_count,
                is_blocklisted, robots_txt_compliant,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4,
                      $5::integer, $5::double precision, $6, $7, true,
                      0, 0, 0, false, true, $8, $8)
            ON CONFLICT (name) DO UPDATE SET
                ats            = COALESCE(NULLIF(companies.ats,''),            EXCLUDED.ats),
                ats_identifier = COALESCE(NULLIF(companies.ats_identifier,''), EXCLUDED.ats_identifier),
                careers_url    = COALESCE(NULLIF(companies.careers_url,''),    EXCLUDED.careers_url),
                active         = true,
                updated_at     = EXCLUDED.updated_at
            RETURNING id
            """,
            name, ats, slug, careers_url,
            50, 360, next_scan, now,
        )
        return row["id"] if row else None
    except Exception:
        return None


async def already_known_slug(conn: asyncpg.Connection,
                             ats: str, slug: str) -> bool:
    row = await conn.fetchval(
        "SELECT 1 FROM companies WHERE ats = $1 AND ats_identifier = $2 LIMIT 1",
        ats, slug,
    )
    return bool(row)
