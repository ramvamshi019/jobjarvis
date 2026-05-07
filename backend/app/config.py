"""Application configuration via pydantic-settings.

All settings have safe defaults so the app starts with zero configuration.
Override via .env file or environment variables.
"""
import secrets
from functools import lru_cache
from typing import List, Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────────────────
    APP_NAME: str = "JobJarvis"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"
    # Auto-generate a secure SECRET_KEY if none provided — safe for local dev
    SECRET_KEY: str = secrets.token_hex(32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # ── Database ─────────────────────────────────────────────────────
    # PostgreSQL is the ONLY supported database.
    # Format: postgresql+asyncpg://user:pass@host:port/db
    DATABASE_URL: str = "postgresql+asyncpg://jobjarvis@localhost:5432/jobjarvis"
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 40

    # ── Redis / Celery ────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # ── AI Providers ─────────────────────────────────────────────────
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    AI_MODEL: str = "gpt-4o-mini"
    AI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    AI_MAX_TOKENS: int = 2000
    AI_TEMPERATURE: float = 0.1

    # ── Cost Control ─────────────────────────────────────────────────
    AI_MIN_FIT_SCORE_FOR_LLM: int = 60
    # AI_SPAM_THRESHOLD is a 0.0–1.0 probability gate: jobs with spam_score
    # at or above this value are excluded from LLM calls.  Kept independent
    # of AI_MIN_FIT_SCORE_FOR_LLM (a 0–100 fit-score threshold) because they
    # measure orthogonal signals.
    AI_SPAM_THRESHOLD: float = 0.6
    AI_DAILY_COST_LIMIT_USD: float = 10.0
    AI_COST_PER_1K_TOKENS: float = 0.0002  # gpt-4o-mini

    # ── Storage ──────────────────────────────────────────────────────
    STORAGE_TYPE: str = "local"  # local | s3
    LOCAL_STORAGE_PATH: str = "./data/storage"
    S3_BUCKET: Optional[str] = None
    S3_REGION: Optional[str] = None
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None

    # ── Scan Settings ─────────────────────────────────────────────────
    SCAN_DEFAULT_TIMEOUT_SECONDS: int = 30
    SCAN_MAX_RETRIES: int = 3
    SCAN_BASE_RETRY_DELAY: float = 2.0
    SCAN_MAX_RETRY_DELAY: float = 60.0
    SCAN_JITTER_MAX: float = 3.0
    RATE_LIMIT_DEFAULT_RPS: float = 1.0  # requests per second per domain

    # ── Notifications ─────────────────────────────────────────────────
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    FROM_EMAIL: str = "noreply@jobjarvis.ai"
    TELEGRAM_BOT_TOKEN: Optional[str] = None

    # ── Notification Thresholds ───────────────────────────────────────
    NOTIFY_MIN_FIT_SCORE: int = 75
    NOTIFY_TARGET_ROLES: List[str] = [
        "AI Engineer", "ML Engineer", "Data Engineer",
        "MLOps Engineer", "Analytics Engineer", "Backend Engineer", "QA/SDET"
    ]

    # ── Embeddings / pgvector ─────────────────────────────────────────
    # sentence_transformers = free local model (all-MiniLM-L6-v2, 384 dims)
    # openai                = text-embedding-3-small (1536 dims, needs API key)
    EMBEDDING_BACKEND: str = "sentence_transformers"
    ST_MODEL: str = "all-MiniLM-L6-v2"       # 384-dim, fast, accurate
    VECTOR_DIMENSIONS: int = 384              # must match ST_MODEL output dims

    # ── CORS ─────────────────────────────────────────────────────────
    # Include both localhost and 127.0.0.1 variants — on macOS, browsers may
    # use either depending on how the dev server URL was opened.
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
