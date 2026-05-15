"""JobJarvis FastAPI application entry point.

Startup lifecycle:
  1. Init DB (create tables, skip pg extensions on SQLite)
  2. Seed sample data if the DB is empty
  3. Serve requests
"""
from contextlib import asynccontextmanager
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db, close_db
from app.api.v1 import (
    health, auth, jobs, companies, applications,
    agent, resumes, admin, scans, reports, outreach,
    matches, ai_assist, extension, profile, drafts,
)
from app.api.v1 import search as search_module
from app.api.v1 import semantic_search as semantic_search_module
from app.api.v1 import analytics as analytics_module
from app.services.realtime_monitor import start_realtime_monitor, stop_realtime_monitor

logger = structlog.get_logger(__name__)


# ── Global system state (set during startup, readable by health endpoint) ──────
_system_state: dict = {
    "db": "unknown",
    "db_type": "postgresql",
    "redis": "disabled",
    "openai": "no_key",
    "anthropic": "no_key",
    "seed": "not_run",
    "jobs_count": 0,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: startup → serve → shutdown."""
    logger.info("startup.begin", app=settings.APP_NAME, version=settings.APP_VERSION,
                db_type=_system_state["db_type"])

    # ── 1. Initialize database (create tables) ────────────────────────────────
    try:
        await init_db()
        _system_state["db"] = "ok"
        logger.info("startup.db_ready", db_type=_system_state["db_type"])
    except Exception as exc:
        _system_state["db"] = f"error: {exc}"
        logger.error("startup.db_failed", error=str(exc))
        # Continue startup anyway — endpoints will return 503 if DB is broken

    # ── 2. Seed sample data if empty ──────────────────────────────────────────
    if _system_state["db"] == "ok":
        try:
            from app.seed import seed_sample_data
            seed_result = await seed_sample_data()
            _system_state["seed"] = seed_result
            if seed_result.get("seeded"):
                logger.info("startup.seed_complete",
                            jobs=seed_result.get("jobs_created"),
                            companies=seed_result.get("companies_created"))
            else:
                logger.info("startup.seed_skipped", reason=seed_result.get("reason"))

            # Record job count for health endpoint
            from app.database import AsyncSessionLocal
            from sqlalchemy import select, func
            from app.models.job import Job
            async with AsyncSessionLocal() as db:
                q = await db.execute(select(func.count(Job.id)).where(Job.active == True))
                _system_state["jobs_count"] = q.scalar() or 0
        except Exception as exc:
            _system_state["seed"] = f"error: {exc}"
            logger.warning("startup.seed_warning", error=str(exc))

    # ── 3. Detect optional services ───────────────────────────────────────────
    # Redis
    try:
        import redis.asyncio as redis_async
        r = redis_async.from_url(settings.REDIS_URL, socket_connect_timeout=1)
        await r.ping()
        await r.aclose()
        _system_state["redis"] = "ok"
    except Exception:
        _system_state["redis"] = "unavailable (Celery tasks disabled)"

    # AI keys
    _system_state["openai"] = "configured" if settings.OPENAI_API_KEY else "no_key (mock responses active)"
    _system_state["anthropic"] = "configured" if settings.ANTHROPIC_API_KEY else "no_key (mock responses active)"

    logger.info("startup.complete",
                db=_system_state["db"],
                redis=_system_state["redis"],
                jobs=_system_state["jobs_count"])

    # ── 4. Start real-time job monitor ──────────────────────────────────────
    if _system_state["db"] == "ok":
        try:
            start_realtime_monitor()
            logger.info("startup.realtime_monitor_started")
        except Exception as exc:
            logger.warning("startup.realtime_monitor_failed", error=str(exc))

    # ── 5. Auto-bootstrap company corpus when small ────────────────────────
    # Two-stage bootstrap on every startup, gated by current corpus size:
    #   • < BOOTSTRAP_TARGET     (default 30k): fire HN + YC + awesome + CC
    #     bulk sources via `bootstrap_all_sources_task`.
    #   • < TECH_SOURCES_TARGET  (default 80k): fire Levels.fyi + Hugging Face
    #     + CNCF + GitHub + Forbes + CBI + AI customers via
    #     `discover_all_tech_sources_task`.
    # Both are idempotent — re-running de-dups on the (ats, slug) and
    # case-insensitive name unique indexes, so it's safe on every startup.
    if _system_state["db"] == "ok" and _system_state["redis"] == "ok":
        try:
            import os as _os_b
            target_bulk = int(_os_b.environ.get("BOOTSTRAP_TARGET",      "30000"))
            target_tech = int(_os_b.environ.get("TECH_SOURCES_TARGET",   "80000"))
            enabled     = _os_b.environ.get("AUTO_BOOTSTRAP", "1").lower() in ("1","true","yes")
            if enabled:
                from app.database import AsyncSessionLocal as _ASL
                from sqlalchemy import select as _sel, func as _func
                from app.models.company import Company as _C
                async with _ASL() as db:
                    cur = (await db.execute(_sel(_func.count(_C.id)).where(_C.active == True))).scalar() or 0

                # Stage A: bulk sources (HN + YC + awesome + Common Crawl)
                # 5-minute delay so the VM finishes booting + Caddy provisions
                # cert + worker forks finish loading ML models BEFORE we pile
                # on 1000s of scan dispatches.  Prevents the SSH-killing CPU
                # spike we kept hitting on smaller machines.
                if cur < target_bulk:
                    from app.workers.bulk_discovery_tasks import bootstrap_all_sources_task
                    bootstrap_all_sources_task.apply_async(countdown=300)
                    logger.info("startup.bootstrap_bulk_queued",
                                companies=cur, target=target_bulk, delay_seconds=300)

                # Stage B: curated tech sources (Levels + HF + CNCF + ...)
                # 10-minute delay — runs AFTER Stage A has had time to drain.
                if cur < target_tech:
                    from app.workers.tech_company_sources import discover_all_tech_sources_task
                    discover_all_tech_sources_task.apply_async(countdown=600)
                    logger.info("startup.bootstrap_tech_queued",
                                companies=cur, target=target_tech, delay_seconds=600)

                if cur >= target_tech:
                    logger.info("startup.bootstrap_skipped_corpus_full",
                                companies=cur, bulk_target=target_bulk, tech_target=target_tech)
        except Exception as exc:
            logger.warning("startup.bootstrap_failed", error=str(exc))

    yield  # ── Application serves requests here ──────────────────────

    # ── Shutdown ────────────────────────────────────────────────────────────
    stop_realtime_monitor()
    await close_db()
    logger.info("shutdown.complete")


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Autonomous AI Career Intelligence Platform — "
        "browses 40,000+ companies, scores job fit, and runs the CareerAgent loop."
    ),
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ───────────────────────────────────────────────────────────────────────
# Localhost is always permitted (dev).  Production origins are derived from:
#   • CORS_ALLOWED_ORIGINS env var (comma-separated full URLs), and
#   • the DOMAIN env var (auto-adds https://DOMAIN and https://www.DOMAIN).
import os as _os
_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
_extra = (_os.environ.get("CORS_ALLOWED_ORIGINS") or "").strip()
if _extra:
    _origins.extend([o.strip() for o in _extra.split(",") if o.strip()])
_domain = (_os.environ.get("DOMAIN") or "").strip()
if _domain:
    _origins.append(f"https://{_domain}")
    _origins.append(f"https://www.{_domain}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
API_PREFIX = "/api"

app.include_router(health.router, prefix=API_PREFIX)
app.include_router(search_module.router, prefix=API_PREFIX)          # public — no auth
app.include_router(semantic_search_module.router, prefix=API_PREFIX) # vector search — no auth
app.include_router(analytics_module.router, prefix=API_PREFIX)       # market analytics — no auth
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(jobs.router, prefix=API_PREFIX)
app.include_router(companies.router, prefix=API_PREFIX)
app.include_router(applications.router, prefix=API_PREFIX)
app.include_router(agent.router, prefix=API_PREFIX)
app.include_router(resumes.router, prefix=API_PREFIX)
app.include_router(matches.router, prefix=API_PREFIX)
app.include_router(ai_assist.router, prefix=API_PREFIX)
app.include_router(extension.router, prefix=API_PREFIX)
app.include_router(profile.router, prefix=API_PREFIX)
app.include_router(drafts.router, prefix=API_PREFIX)
app.include_router(admin.router, prefix=API_PREFIX)
app.include_router(scans.router, prefix=API_PREFIX)
app.include_router(reports.router, prefix=API_PREFIX)
app.include_router(outreach.router, prefix=API_PREFIX)


# ── Root and system routes ─────────────────────────────────────────────────────
@app.get("/", tags=["root"])
async def root():
    """Root endpoint — confirms the server is running."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs",
        "health": "/api/health",
    }


@app.get("/api/system", tags=["system"], include_in_schema=True)
async def system_status():
    """
    Full system status including DB, Redis, AI keys, and seed state.
    No authentication required — safe for monitoring probes.
    """
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        **_system_state,
    }
