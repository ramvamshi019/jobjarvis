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


# ── Task lifecycle observability ────────────────────────────────────────────
# These three handlers print a one-line summary for every task that
# enters or leaves the worker, regardless of what the task itself logs.
# Combined with PYTHONUNBUFFERED=1 in docker-compose, this makes
# `docker compose logs celery_worker` actually show what's happening.

_task_log = structlog.get_logger("celery.lifecycle")


@signals.task_received.connect
def _on_task_received(sender=None, request=None, **kwargs):
    name = getattr(request, "task", "?")
    tid = getattr(request, "id", "?")
    _task_log.info("task_received", task=name, id=tid)


@signals.task_success.connect
def _on_task_success(sender=None, result=None, **kwargs):
    name = getattr(sender, "name", "?")
    tid = getattr(getattr(sender, "request", None), "id", "?")
    _task_log.info("task_success", task=name, id=tid)


@signals.task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None, **kwargs):
    name = getattr(sender, "name", "?")
    _task_log.error(
        "task_failure",
        task=name,
        id=task_id,
        error_type=type(exception).__name__ if exception else "?",
        error=str(exception) if exception else "?",
    )

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
        "app.workers.notification_tasks",
        "app.workers.auto_apply_tasks",
        "app.workers.cities_discovery_tasks",
        "app.workers.backup_tasks",
        "app.workers.megaemployer_tasks",
        "app.workers.extra_job_sources",
        "app.workers.vc_portfolio_discovery",
        "app.workers.ai_company_discovery",
        "app.workers.ats_promoter_tasks",
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
        # Tier4 (priority < 20): every 6 h — rescues the long tail so a
        # company (often a real tech company that just hasn't been scanned
        # yet) is never permanently invisible to the scanner.
        "scan-tier4-companies": {
            "task": "app.workers.scan_tasks.scan_tier_companies",
            "schedule": crontab(minute=45, hour="*/6"),
            "args": ("tier4",),
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
        # 30 US-city Wikidata + Built In sweep: weekly Sunday at 2 AM UTC
        "discover-us-cities": {
            "task": "app.workers.cities_discovery_tasks.discover_us_cities",
            "schedule": crontab(hour=2, minute=0, day_of_week=0),
        },
        # Daily Postgres backup at 03:30 UTC — keeps 7 days of dumps in
        # /app/backups (persistent volume).
        "daily-db-backup": {
            "task": "app.workers.backup_tasks.backup_database",
            "schedule": crontab(hour=3, minute=30),
        },

        # ── Extra job sources ────────────────────────────────────────────
        # USAJobs.gov — federal/state tech jobs.  Every 30 min.
        "fetch-usajobs": {
            "task": "app.workers.extra_job_sources.fetch_usajobs",
            "schedule": crontab(minute="5,35"),
        },
        # Reddit r/forhire & r/remotejs — niche tech listings.  Every 30 min.
        "fetch-reddit-hiring": {
            "task": "app.workers.extra_job_sources.fetch_reddit",
            "schedule": crontab(minute="10,40"),
        },
        # HN Show HN / Launch HN  — discover hiring startups.  Daily 04:30 UTC.
        "discover-hn-launches": {
            "task": "app.workers.extra_job_sources.discover_hn_launches",
            "schedule": crontab(hour=4, minute=30),
        },
        # GitHub Trending — discover hiring orgs.  Daily 05:00 UTC.
        "discover-github-trending": {
            "task": "app.workers.extra_job_sources.discover_github_trending",
            "schedule": crontab(hour=5, minute=0),
        },
        # VC portfolio sweep (16 top VCs) — weekly Sunday 03:00 UTC.
        # Yields high-quality Series A+ startups that are actively hiring.
        "discover-vc-portfolios": {
            "task": "app.workers.vc_portfolio_discovery.discover_vc_portfolios",
            "schedule": crontab(hour=3, minute=0, day_of_week=0),
        },
        # TechCrunch funding RSS — every 30 min, captures newly funded
        # companies as they get announced.
        "discover-techcrunch-funding": {
            "task": "app.workers.vc_portfolio_discovery.discover_techcrunch_funding",
            "schedule": crontab(minute="15,45"),
        },

        # ── AI-driven company discovery ──────────────────────────────────
        # Every hour, Claude picks the next theme from a rotating list of
        # ~45 themes (industries, cities, stages, tech-skills) and suggests
        # 40-60 hiring US companies.  Each is probed for ATS + upserted.
        # At ~$0.015 per call, ~$11/month total cost.
        # Tech-targeted company discovery — every 30 min (was hourly). This
        # is the highest-quality tech source (Claude picks hiring US tech
        # companies); doubling cadence ≈ doubles new tech companies/day.
        # Cost ≈ doubles to ~$22/month.
        "ai-discover-companies": {
            "task": "app.workers.ai_company_discovery.discover_via_ai",
            "schedule": crontab(minute="25,55"),
        },
        # ATS auto-promoter — picks 50 ats=unknown companies every 30 min,
        # fetches their careers page, looks for embedded Greenhouse / Lever /
        # Workday / etc. links, and auto-upgrades them.  Converts ~50-70% of
        # unknowns into scannable companies over time.
        "promote-unknown-companies": {
            "task": "app.workers.ats_promoter_tasks.promote_unknown_companies",
            "schedule": crontab(minute="0,30"),
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
        # RemoteOK, The Muse, Arbeitnow, HN Hiring, Adzuna: every 30 min
        "fetch-job-boards": {
            "task": "app.workers.jobboard_tasks.fetch_all_boards",
            "schedule": crontab(minute="*/30"),
        },
        # Big-volume employers (Amazon, Microsoft, Apple, Google, Stripe):
        # DISABLED — their public API endpoints changed since this code was
        # written, all currently return 0.  Re-enable once URLs are updated.
        # Tracked work item: probe live endpoints and update megaemployer_tasks.py.
        #
        # "fetch-megaemployers": {
        #     "task": "app.workers.megaemployer_tasks.fetch_all_megaemployers",
        #     "schedule": crontab(minute=20, hour="*/1"),
        # },

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

        # ── Real-time match alerts ────────────────────────────────────────
        # Push Slack/email when new top-10 matches appear (every 15 min)
        "push-match-alerts": {
            "task": "app.workers.notification_tasks.push_new_match_notifications",
            "schedule": crontab(minute="*/15"),
        },
    },
)
