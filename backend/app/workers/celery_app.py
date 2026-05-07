"""Celery application configuration."""
import logging
import structlog
from celery import Celery, signals
from celery.schedules import crontab
from app.config import settings


def _configure_structlog(loglevel: int = logging.INFO):
    """Bridge structlog → stdlib logging so Celery's --loglevel filter applies.

    Uses stdlib.LoggerFactory + stdlib.BoundLogger so every structlog call
    (logger.debug / .info / .warning) maps to the matching stdlib level.
    Celery's root logger level then filters correctly — no wrap_for_formatter
    complexity needed.
    """
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,       # honour stdlib level filter
            structlog.stdlib.add_log_level,          # inject 'level' key
            structlog.stdlib.add_logger_name,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(   # plain key=value output
                key_order=["level", "event"],
                drop_missing=True,
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    # Propagate the Celery log level to the root logger so filter_by_level works
    logging.getLogger().setLevel(loglevel)
    # Silence noisy HTTP/async libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)


@signals.setup_logging.connect
def _on_setup_logging(loglevel, logfile, format, colorize, **kwargs):
    """Called once when the worker/beat process configures logging.

    loglevel here is an int (e.g. logging.INFO = 20).
    We layer structlog on top after Celery sets up its handlers.
    """
    _configure_structlog(loglevel=loglevel)


@signals.worker_process_init.connect
def _on_worker_init(**kwargs):
    """Called in each *forked* child process — re-run configure after fork."""
    # Re-read the root logger level that was set by setup_logging in the parent
    _configure_structlog(loglevel=logging.getLogger().level or logging.INFO)

celery_app = Celery(
    "jobjarvis",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.scan_tasks",
        "app.workers.ai_tasks",
        "app.workers.discovery_tasks",
        "app.workers.healer_tasks",
        "app.workers.embedding_tasks",
        "app.workers.ml_tasks",
        "app.workers.jobboard_tasks",
        "app.workers.ats_directory_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.workers.scan_tasks.*":             {"queue": "scans"},
        "app.workers.ai_tasks.*":               {"queue": "ai"},
        "app.workers.discovery_tasks.*":        {"queue": "scans"},
        "app.workers.healer_tasks.*":           {"queue": "scans"},
        "app.workers.embedding_tasks.*":        {"queue": "ai"},
        "app.workers.ml_tasks.*":               {"queue": "ai"},
        "app.workers.ats_directory_tasks.*":    {"queue": "scans"},
    },
    beat_schedule={

        # ── Job scanning (near real-time) ─────────────────────────────────
        # Tier1 (priority ≥ 90): every 10 min — Google, Stripe, top companies
        "scan-tier1-companies": {
            "task": "app.workers.scan_tasks.scan_tier_companies",
            "schedule": crontab(minute="*/10"),
            "args": ("tier1",),
        },
        # Tier2 (priority 60–89): every hour
        "scan-tier2-companies": {
            "task": "app.workers.scan_tasks.scan_tier_companies",
            "schedule": crontab(minute=5, hour="*/1"),
            "args": ("tier2",),
        },
        # Tier3 (priority 20–59): every 3 hours
        "scan-tier3-companies": {
            "task": "app.workers.scan_tasks.scan_tier_companies",
            "schedule": crontab(minute=15, hour="*/3"),
            "args": ("tier3",),
        },
        # Newly discovered companies: scan every 30 min regardless of tier
        "scan-new-companies": {
            "task": "app.workers.scan_tasks.scan_new_companies",
            "schedule": crontab(minute="*/30"),
        },
        # Auto-promote companies based on hiring activity: daily at 6am
        "promote-active-companies": {
            "task": "app.workers.scan_tasks.promote_active_companies",
            "schedule": crontab(hour=6, minute=0),
        },

        # ── Company discovery (fully automated) ───────────────────────────
        # ATS directory probe: every 4 hours — grows company list automatically
        "ingest-ats-directories": {
            "task": "app.workers.ats_directory_tasks.ingest_ats_directories",
            "schedule": crontab(minute=0, hour="*/4"),
        },
        # Quick slug-guess discovery: every 2 hours
        "discover-companies-quick": {
            "task": "app.workers.discovery_tasks.discover_companies_quick",
            "schedule": crontab(minute=30, hour="*/2"),
        },
        # Full deep discovery: every 3 days (probes 48k+ candidates)
        "discover-companies-full": {
            "task": "app.workers.discovery_tasks.discover_companies_task",
            "schedule": crontab(hour=3, minute=0, day_of_week="0,3,6"),
        },

        # ── AI auto-healer ────────────────────────────────────────────────
        # Fixes broken ATS connectors automatically every morning
        "heal-failing-companies": {
            "task": "app.workers.healer_tasks.heal_failing_companies",
            "schedule": crontab(hour=7, minute=0),
        },

        # ── Embedding pipeline ────────────────────────────────────────────
        # Runs every 15 min — keeps semantic search fresh
        "embed-new-jobs": {
            "task": "app.workers.embedding_tasks.embed_new_jobs",
            "schedule": crontab(minute="*/15"),
        },

        # ── ML pipeline ───────────────────────────────────────────────────
        "predict-salaries": {
            "task": "app.workers.ml_tasks.predict_missing_salaries",
            "schedule": crontab(hour=8, minute=0),
        },
        "deduplicate-jobs": {
            "task": "app.workers.ml_tasks.deduplicate_jobs",
            "schedule": crontab(hour=8, minute=30),
        },
        "detect-hiring-spikes": {
            "task": "app.workers.ml_tasks.detect_hiring_spikes",
            "schedule": crontab(hour=9, minute=0),
        },
        "train-salary-model": {
            "task": "app.workers.ml_tasks.train_salary_model",
            "schedule": crontab(hour=5, minute=0, day_of_week=0),
        },

        # ── Job board APIs ────────────────────────────────────────────────
        # RemoteOK, The Muse, Arbeitnow, HN Hiring, Adzuna: every 2 hours
        "fetch-job-boards": {
            "task": "app.workers.jobboard_tasks.fetch_all_boards",
            "schedule": crontab(minute=45, hour="*/2"),
        },

        # ── AI / intelligence tasks ───────────────────────────────────────
        "run-career-agent": {
            "task": "app.workers.ai_tasks.run_career_agent_all_users",
            "schedule": crontab(minute=15),
        },
        "data-quality-check": {
            "task": "app.workers.ai_tasks.run_data_quality",
            "schedule": crontab(hour=3, minute=0),
        },
        "update-market-trends": {
            "task": "app.workers.ai_tasks.update_company_intelligence",
            "schedule": crontab(hour=4, minute=0),
        },
        "self-correction": {
            "task": "app.workers.ai_tasks.run_self_correction_all_users",
            "schedule": crontab(hour=5, minute=0),
        },
    },
)
