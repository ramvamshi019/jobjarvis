"""
PostgreSQL Database Configuration.
Enforces PostgreSQL as the only supported database.
"""
import logging
import os
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text, BigInteger
from sqlalchemy.pool import NullPool

from app.config import settings

logger = logging.getLogger(__name__)

# Enforce PostgreSQL
if "postgresql" not in settings.DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be a PostgreSQL connection string.")

# Use NullPool in Celery workers to avoid "Future attached to a different loop" errors.
# Each asyncio.run() creates a fresh event loop; pooled connections belong to the old
# loop and cause RuntimeError on reuse.  NullPool opens a fresh connection per request
# and closes it immediately — no cross-loop state.
_in_celery_worker = bool(os.environ.get("CELERY_WORKER"))

if _in_celery_worker:
    async_engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        poolclass=NullPool,
    )
else:
    # Engine configuration
    async_engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

class Base(DeclarativeBase):
    """Shared base for all ORM models."""
    pass

# Custom BigInt primary key for PostgreSQL
BigIntPK = BigInteger

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

@asynccontextmanager
async def get_db_context():
    """Async context manager for a database session (use outside of FastAPI DI)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db() -> None:
    """Initialize DB: enable extensions and create all tables."""
    import app.models  # noqa: F401

    # Enable required PostgreSQL extensions
    async with async_engine.connect() as conn:
        for ext in ("vector", "pg_trgm"):
            try:
                async with conn.begin():
                    await conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
                    logger.info("db.extension_ready ext=%s", ext)
            except Exception as e:
                logger.warning("db.extension_skipped ext=%s error=%s", ext, e)

    # Create all tables that don't exist yet (idempotent)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("db.tables_ready")

    # ── Scale-out indexes ─────────────────────────────────────────────────
    # Partial indexes filtered to `active=true` make the hot analytics +
    # scheduler queries fast at 100k+ rows.  We use plain `CREATE INDEX IF
    # NOT EXISTS` (not CONCURRENTLY) because:
    #   - CONCURRENTLY deadlocks against active scans hitting the same tables
    #   - On a fresh DB the indexes build in <1s; on a populated DB they
    #     build in <10s and block writers only briefly
    #   - Subsequent boots short-circuit on IF NOT EXISTS — no rebuild cost
    # If you ever need a true zero-downtime build on a huge live DB, run
    # CONCURRENTLY manually via psql during a quiet window.
    _PARTIAL_INDEXES = [
        ("ix_jobs_active_first_seen_partial",
         "CREATE INDEX IF NOT EXISTS ix_jobs_active_first_seen_partial "
         "ON jobs (first_seen_at DESC) WHERE active = true"),
        ("ix_jobs_active_posted_at_partial",
         "CREATE INDEX IF NOT EXISTS ix_jobs_active_posted_at_partial "
         "ON jobs (posted_at DESC NULLS LAST) WHERE active = true"),
        ("ix_jobs_active_role_partial",
         "CREATE INDEX IF NOT EXISTS ix_jobs_active_role_partial "
         "ON jobs (role_category, country) WHERE active = true"),
        ("ix_companies_active_next_scan_partial",
         "CREATE INDEX IF NOT EXISTS ix_companies_active_next_scan_partial "
         "ON companies (priority_score DESC, next_scan_at NULLS FIRST) "
         "WHERE active = true AND is_blocklisted = false"),
        ("ix_companies_active_created_at_partial",
         "CREATE INDEX IF NOT EXISTS ix_companies_active_created_at_partial "
         "ON companies (created_at DESC) WHERE active = true"),
    ]
    async with async_engine.begin() as conn:
        for name, sql in _PARTIAL_INDEXES:
            try:
                await conn.execute(text(sql))
                logger.info("db.index_ready name=%s", name)
            except Exception as e:
                logger.warning("db.index_skipped name=%s error=%s", name, str(e)[:120])

    # ── Schema migrations (idempotent ADD COLUMN IF NOT EXISTS) ───────────
    # We don't use Alembic; instead we ALTER existing tables with
    # `IF NOT EXISTS` so old prod DBs get the new columns on next boot
    # without losing data.
    _SCHEMA_PATCHES = [
        # Apply-queue flow columns on `applications`
        ("applications.tailored_resume_md",
         "ALTER TABLE applications ADD COLUMN IF NOT EXISTS tailored_resume_md TEXT"),
        ("applications.fit_score",
         "ALTER TABLE applications ADD COLUMN IF NOT EXISTS fit_score INTEGER"),
        ("applications.fit_summary_json",
         "ALTER TABLE applications ADD COLUMN IF NOT EXISTS fit_summary_json JSONB"),
        ("applications.tailoring_error",
         "ALTER TABLE applications ADD COLUMN IF NOT EXISTS tailoring_error TEXT"),
    ]
    async with async_engine.begin() as conn:
        for name, sql in _SCHEMA_PATCHES:
            try:
                await conn.execute(text(sql))
                logger.info("db.column_ready name=%s", name)
            except Exception as e:
                logger.warning("db.column_skipped name=%s error=%s", name, str(e)[:120])

async def close_db() -> None:
    await async_engine.dispose()
