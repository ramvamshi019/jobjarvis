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

async def close_db() -> None:
    await async_engine.dispose()
