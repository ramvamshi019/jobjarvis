"""
SQLite database layer for job storage, tracking, and analytics.

Features:
- Context-managed connections (no leaked connections)
- WAL mode for concurrent reads
- Full-text search via FTS5
- Schema versioning with auto-migration
- Analytics queries for dashboard charts
"""

import sqlite3
import os
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional

from config import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2


# ─── Connection Management ──────────────────────────────────────

@contextmanager
def get_connection():
    """Context-managed database connection. Always commits on success, rolls back on error."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Schema & Migrations ───────────────────────────────────────

def init_db():
    """Initialize database schema with auto-migration support."""
    with get_connection() as conn:
        # Create version tracking table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT DEFAULT (datetime('now'))
            )
        """)

        current_version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0] or 0

        if current_version < 1:
            _migrate_v1(conn)
        if current_version < 2:
            _migrate_v2(conn)

    logger.info(f"Database initialized (schema v{SCHEMA_VERSION})")


def _migrate_v1(conn):
    """V1: Core jobs table with indexes."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          TEXT UNIQUE NOT NULL,
            source          TEXT NOT NULL,
            company         TEXT NOT NULL,
            title           TEXT NOT NULL,
            description     TEXT,
            location        TEXT,
            link            TEXT NOT NULL,
            salary_min      REAL,
            salary_max      REAL,
            salary_currency TEXT DEFAULT 'USD',
            experience_min  INTEGER,
            experience_max  INTEGER,
            match_score     REAL DEFAULT 0.0,
            resume_path     TEXT,
            cover_letter    TEXT,
            applied_status  TEXT DEFAULT 'new',
            alert_sent      INTEGER DEFAULT 0,
            notes           TEXT,
            tags            TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(applied_status);
        CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(match_score DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
        CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
    """)
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")
    logger.info("Applied migration v1: core schema")


def _migrate_v2(conn):
    """V2: Full-text search + pipeline_runs tracking."""
    # FTS5 for fast text search
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
            title, company, description, location,
            content='jobs',
            content_rowid='id'
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
            INSERT INTO jobs_fts(rowid, title, company, description, location)
            VALUES (new.id, new.title, new.company, new.description, new.location);
        END;

        CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
            INSERT INTO jobs_fts(jobs_fts, rowid, title, company, description, location)
            VALUES ('delete', old.id, old.title, old.company, old.description, old.location);
        END;

        CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
            INSERT INTO jobs_fts(jobs_fts, rowid, title, company, description, location)
            VALUES ('delete', old.id, old.title, old.company, old.description, old.location);
            INSERT INTO jobs_fts(rowid, title, company, description, location)
            VALUES (new.id, new.title, new.company, new.description, new.location);
        END;

        -- Pipeline run history
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            new_jobs    INTEGER DEFAULT 0,
            total_fetched INTEGER DEFAULT 0,
            resumes_gen INTEGER DEFAULT 0,
            alerts_sent INTEGER DEFAULT 0,
            errors      TEXT,
            duration_s  REAL,
            status      TEXT DEFAULT 'running'
        );
    """)

    # Rebuild FTS index from existing data
    try:
        conn.execute("""
            INSERT INTO jobs_fts(rowid, title, company, description, location)
            SELECT id, title, company, description, location FROM jobs
        """)
    except sqlite3.IntegrityError:
        pass  # FTS already populated

    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (2)")
    logger.info("Applied migration v2: FTS5 + pipeline_runs")


# ─── Core CRUD ──────────────────────────────────────────────────

def job_exists(job_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row is not None


def insert_job(job: dict) -> bool:
    """Insert a single job. Returns True if inserted, False if duplicate."""
    if job_exists(job["job_id"]):
        return False
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO jobs (job_id, source, company, title, description, location,
                                link, match_score, salary_min, salary_max, experience_min, experience_max)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job["job_id"], job["source"], job["company"], job["title"],
                job.get("description", ""), job.get("location", ""),
                job["link"], job.get("match_score", 0.0),
                job.get("salary_min"), job.get("salary_max"),
                job.get("experience_min"), job.get("experience_max"),
            ),
        )
    return True


def bulk_insert_jobs(jobs: list[dict]) -> int:
    """Insert many jobs efficiently. Returns count of newly inserted."""
    if not jobs:
        return 0
    with get_connection() as conn:
        inserted = 0
        for job in jobs:
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO jobs
                       (job_id, source, company, title, description, location,
                        link, match_score, salary_min, salary_max, experience_min, experience_max)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job["job_id"], job["source"], job["company"], job["title"],
                        job.get("description", ""), job.get("location", ""),
                        job["link"], job.get("match_score", 0.0),
                        job.get("salary_min"), job.get("salary_max"),
                        job.get("experience_min"), job.get("experience_max"),
                    ),
                )
                if cur.rowcount == 1:
                    inserted += 1
            except sqlite3.IntegrityError:
                continue
    logger.info(f"Bulk insert: {inserted}/{len(jobs)} new jobs")
    return inserted


# ─── Update Operations ──────────────────────────────────────────

def update_resume_path(job_id: str, resume_path: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET resume_path = ?, updated_at = ? WHERE job_id = ?",
            (resume_path, datetime.now().isoformat(), job_id),
        )


def update_cover_letter(job_id: str, cover_letter_path: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET cover_letter = ?, updated_at = ? WHERE job_id = ?",
            (cover_letter_path, datetime.now().isoformat(), job_id),
        )


def update_applied_status(job_id: str, status: str):
    valid = {"new", "saved", "applied", "interviewing", "rejected", "offer", "archived"}
    if status not in valid:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {valid}")
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET applied_status = ?, updated_at = ? WHERE job_id = ?",
            (status, datetime.now().isoformat(), job_id),
        )


def update_job_notes(job_id: str, notes: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET notes = ?, updated_at = ? WHERE job_id = ?",
            (notes, datetime.now().isoformat(), job_id),
        )


def update_job_tags(job_id: str, tags: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET tags = ?, updated_at = ? WHERE job_id = ?",
            (tags, datetime.now().isoformat(), job_id),
        )


def mark_alert_sent(job_id: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET alert_sent = 1, updated_at = ? WHERE job_id = ?",
            (datetime.now().isoformat(), job_id),
        )


def delete_job(job_id: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


def bulk_update_status(job_ids: list[str], status: str):
    """Update status for multiple jobs at once."""
    valid = {"new", "saved", "applied", "interviewing", "rejected", "offer", "archived"}
    if status not in valid:
        raise ValueError(f"Invalid status '{status}'.")
    with get_connection() as conn:
        now = datetime.now().isoformat()
        for jid in job_ids:
            conn.execute(
                "UPDATE jobs SET applied_status = ?, updated_at = ? WHERE job_id = ?",
                (status, now, jid),
            )


# ─── Query Operations ──────────────────────────────────────────

def get_all_jobs(limit: int = 500, offset: int = 0) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def get_jobs_by_status(status: str, limit: int = 500) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE applied_status = ? ORDER BY match_score DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_unsent_alerts() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE alert_sent = 0 AND match_score > 0 ORDER BY match_score DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_jobs_without_resume(min_score: float = 50.0) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE resume_path IS NULL AND match_score >= ?
               ORDER BY match_score DESC""",
            (min_score,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_job_by_id(job_id: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_job_count() -> dict:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        new = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_status='new'").fetchone()[0]
        saved = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_status='saved'").fetchone()[0]
        with_resume = conn.execute("SELECT COUNT(*) FROM jobs WHERE resume_path IS NOT NULL").fetchone()[0]
        applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_status='applied'").fetchone()[0]
        interviewing = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_status='interviewing'").fetchone()[0]
        offers = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_status='offer'").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_status='rejected'").fetchone()[0]
    return {
        "total": total, "new": new, "saved": saved,
        "with_resume": with_resume, "applied": applied,
        "interviewing": interviewing, "offers": offers, "rejected": rejected,
    }


# ─── Full-Text Search ──────────────────────────────────────────

def search_jobs(query: str, limit: int = 100) -> list[dict]:
    """Full-text search across title, company, description, location."""
    with get_connection() as conn:
        # Try FTS5 first, fall back to LIKE if FTS table doesn't exist
        try:
            rows = conn.execute(
                """SELECT jobs.* FROM jobs
                   JOIN jobs_fts ON jobs.id = jobs_fts.rowid
                   WHERE jobs_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS table might not exist yet — fallback to LIKE
            like = f"%{query}%"
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE title LIKE ? OR company LIKE ? OR description LIKE ? OR location LIKE ?
                   ORDER BY match_score DESC LIMIT ?""",
                (like, like, like, like, limit),
            ).fetchall()
    return [dict(r) for r in rows]


# ─── Analytics Queries ──────────────────────────────────────────

def get_score_distribution() -> list[dict]:
    """Get job count by score ranges for histogram."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                CASE
                    WHEN match_score >= 90 THEN '90-100'
                    WHEN match_score >= 80 THEN '80-89'
                    WHEN match_score >= 70 THEN '70-79'
                    WHEN match_score >= 60 THEN '60-69'
                    WHEN match_score >= 50 THEN '50-59'
                    WHEN match_score >= 40 THEN '40-49'
                    WHEN match_score >= 30 THEN '30-39'
                    ELSE '0-29'
                END as score_range,
                COUNT(*) as count
            FROM jobs
            GROUP BY score_range
            ORDER BY score_range DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_jobs_by_source() -> list[dict]:
    """Get job counts grouped by source."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT source, COUNT(*) as count
            FROM jobs GROUP BY source ORDER BY count DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_jobs_by_company(limit: int = 20) -> list[dict]:
    """Get top companies by job count."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT company, COUNT(*) as count, ROUND(AVG(match_score), 1) as avg_score
            FROM jobs GROUP BY company ORDER BY count DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_jobs_over_time(days: int = 30) -> list[dict]:
    """Get daily job counts for the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM jobs
            WHERE created_at >= ?
            GROUP BY DATE(created_at)
            ORDER BY date
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def get_status_breakdown() -> list[dict]:
    """Get job counts by status for pie chart."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT applied_status as status, COUNT(*) as count
            FROM jobs GROUP BY applied_status ORDER BY count DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_top_scoring_jobs(limit: int = 10) -> list[dict]:
    """Get the highest scoring jobs."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY match_score DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_jobs(hours: int = 24, limit: int = 50) -> list[dict]:
    """Get jobs added in the last N hours."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE created_at >= ? ORDER BY match_score DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Pipeline Run Tracking ──────────────────────────────────────

def log_pipeline_start() -> int:
    """Log a pipeline run start. Returns the run ID."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO pipeline_runs (started_at) VALUES (?)",
            (datetime.now().isoformat(),),
        )
        return cursor.lastrowid


def log_pipeline_end(run_id: int, stats: dict):
    """Log pipeline run completion with stats."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE pipeline_runs SET
                finished_at = ?, new_jobs = ?, total_fetched = ?,
                resumes_gen = ?, alerts_sent = ?, errors = ?,
                duration_s = ?, status = ?
               WHERE id = ?""",
            (
                datetime.now().isoformat(),
                stats.get("new_jobs", 0),
                stats.get("total_fetched", 0),
                stats.get("resumes_generated", 0),
                stats.get("alerts_sent", 0),
                str(stats.get("errors", [])) if stats.get("errors") else None,
                stats.get("duration", 0),
                "completed" if not stats.get("errors") else "completed_with_errors",
                run_id,
            ),
        )


def get_pipeline_history(limit: int = 20) -> list[dict]:
    """Get recent pipeline run history."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
