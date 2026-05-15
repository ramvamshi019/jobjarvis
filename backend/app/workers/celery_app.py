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
        "app.workers.bulk_discovery_tasks",
        "app.workers.tech_company_sources",
        "app.workers.maintenance_tasks",
        "app.workers.jobspy_tasks",
        "app.workers.newgrad_sources",
        "app.workers.application_tasks",
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
        "app.workers.bulk_discovery_tasks.*":   {"queue": "scans"},
        "app.workers.tech_company_sources.*":   {"queue": "scans"},
        "app.workers.maintenance_tasks.*":      {"queue": "scans"},
        "app.workers.jobspy_tasks.*":           {"queue": "scans"},
        "app.workers.newgrad_sources.*":        {"queue": "scans"},
        "app.workers.application_tasks.*":      {"queue": "ai"},
    },
    task_default_queue="default",
    beat_schedule={

        # ── Job scanning (near real-time) ─────────────────────────────────
        # Tier1 (priority ≥ 90): every 5 min — Stripe, top companies.
        # Aggressive cadence: when worker concurrency is high enough this
        # tier finishes well under 5 min, so new postings on top employers
        # land in the DB ~minutes after the ATS publishes them.
        "scan-tier1-companies": {
            "task": "app.workers.scan_tasks.scan_tier_companies",
            "schedule": crontab(minute="*/5"),
            "args": ("tier1",),
        },
        # Tier2 (priority 60–89): every 15 min.
        "scan-tier2-companies": {
            "task": "app.workers.scan_tasks.scan_tier_companies",
            "schedule": crontab(minute="2,17,32,47"),
            "args": ("tier2",),
        },
        # Tier3 (priority 20–59): every hour.
        "scan-tier3-companies": {
            "task": "app.workers.scan_tasks.scan_tier_companies",
            "schedule": crontab(minute=8, hour="*/1"),
            "args": ("tier3",),
        },
        # Newly discovered companies: scan every 5 min.
        # Tight cadence here means a company discovered at minute :01 has
        # its jobs ingested by :06 at the latest — important for keeping
        # the new-company funnel hot.
        "scan-new-companies": {
            "task": "app.workers.scan_tasks.scan_new_companies",
            "schedule": crontab(minute="*/5"),
        },
        # Auto-promote companies based on hiring activity: every 3 hours
        # (was daily — quicker promotion of newly active companies to tier1).
        "promote-active-companies": {
            "task": "app.workers.scan_tasks.promote_active_companies",
            "schedule": crontab(minute=20, hour="*/3"),
        },

        # ── Company discovery (fully automated) ───────────────────────────
        # ATS directory probe: every hour — grows company list automatically.
        # Was every 4h.  Each pass probes ~1k known slugs across all ATSes;
        # cheap when most are already in DB (de-duped early).
        "ingest-ats-directories": {
            "task": "app.workers.ats_directory_tasks.ingest_ats_directories",
            "schedule": crontab(minute=0, hour="*/1"),
        },
        # Quick slug-guess discovery: every 30 min (was every 2 hours).
        "discover-companies-quick": {
            "task": "app.workers.discovery_tasks.discover_companies_quick",
            "schedule": crontab(minute="*/30"),
        },
        # Full deep discovery: nightly at 3:00 UTC (probes 48k+ candidates).
        # Was 3×/week — bumped to nightly to accelerate corpus bootstrap.
        # Idempotent: candidates already in DB are skipped, so churn is cheap.
        "discover-companies-full": {
            "task": "app.workers.discovery_tasks.discover_companies_task",
            "schedule": crontab(hour=3, minute=0),
        },

        # ── Bulk one-shot sources (wrap standalone scripts) ──────────────
        # HN "Who is hiring" monthly threads — every day at 04:00 UTC.
        # 3-5k US tech companies per full pass; running daily catches any new
        # companies that newly-archived threads expose.
        "bulk-discover-hn": {
            "task": "app.workers.bulk_discovery_tasks.discover_hn_whoshiring",
            "schedule": crontab(hour=4, minute=0),
        },
        # YC complete batches — weekly, every Sunday 04:30 UTC.
        "bulk-discover-yc": {
            "task": "app.workers.bulk_discovery_tasks.discover_yc_complete",
            "schedule": crontab(hour=4, minute=30, day_of_week=0),
        },
        # Awesome-lists curated repos — weekly Monday 05:00 UTC.
        "bulk-discover-awesome": {
            "task": "app.workers.bulk_discovery_tasks.discover_awesome_lists",
            "schedule": crontab(hour=5, minute=0, day_of_week=1),
        },
        # Common Crawl CDX over known ATS hosts — weekly Tuesday 05:30 UTC.
        # Heaviest source (5-15k companies per pass); weekly cadence prevents
        # CC index hammering while still keeping the corpus current.
        "bulk-discover-common-crawl": {
            "task": "app.workers.bulk_discovery_tasks.discover_known_lists",
            "schedule": crontab(hour=5, minute=30, day_of_week=2),
        },

        # ── Curated tech-only sources (US tech employers) ────────────────
        # Levels.fyi sitemap — every company that pays software engineers.
        # Daily at 06:00 UTC; cheap (~5k slugs, sitemap-driven).
        "tech-discover-levels-fyi": {
            "task": "app.workers.tech_company_sources.discover_levels_fyi",
            "schedule": crontab(hour=6, minute=0),
        },
        # Hugging Face orgs — every public AI/ML organization.  Weekly Wed.
        "tech-discover-huggingface": {
            "task": "app.workers.tech_company_sources.discover_huggingface",
            "schedule": crontab(hour=6, minute=15, day_of_week=3),
        },
        # CNCF landscape — cloud-native ecosystem members.  Weekly Thu.
        "tech-discover-cncf": {
            "task": "app.workers.tech_company_sources.discover_cncf",
            "schedule": crontab(hour=6, minute=30, day_of_week=4),
        },
        # GitHub top orgs — public-software employers.  Weekly Fri.
        "tech-discover-github-top-orgs": {
            "task": "app.workers.tech_company_sources.discover_github_top_orgs",
            "schedule": crontab(hour=6, minute=45, day_of_week=5),
        },
        # Forbes Cloud 100 / AI 50 / Fintech 50 — monthly on the 1st.
        "tech-discover-forbes-lists": {
            "task": "app.workers.tech_company_sources.discover_forbes_lists",
            "schedule": crontab(hour=7, minute=0, day_of_month="1"),
        },
        # CB Insights tech unicorns — weekly Sat 07:15 UTC.
        "tech-discover-cb-unicorns": {
            "task": "app.workers.tech_company_sources.discover_cb_unicorns",
            "schedule": crontab(hour=7, minute=15, day_of_week=6),
        },
        # OpenAI + Anthropic customer logos — weekly Sun 07:30 UTC.
        "tech-discover-ai-customers": {
            "task": "app.workers.tech_company_sources.discover_ai_customers",
            "schedule": crontab(hour=7, minute=30, day_of_week=0),
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

        # ── New-grad-only curated GitHub sources ─────────────────────────
        # All three repos are maintained daily by community contributors
        # and ONLY contain entry-level / new-grad / intern positions.
        # Highest signal-to-noise for the target user (MS new grad).
        "newgrad-simplify-fulltime": {
            "task": "app.workers.newgrad_sources.fetch_simplify_newgrad",
            "schedule": crontab(minute=5, hour="*/2"),
        },
        "newgrad-simplify-internships": {
            "task": "app.workers.newgrad_sources.fetch_simplify_summer2026",
            "schedule": crontab(minute=15, hour="*/2"),
        },
        "newgrad-vanshb03": {
            "task": "app.workers.newgrad_sources.fetch_vanshb03_newgrad",
            "schedule": crontab(minute=25, hour="*/4"),
        },

        # ── Multi-board scraping via python-jobspy ────────────────────────
        # Covers mega-employers (Amazon, Google, Apple, Meta) whose own
        # portals are locked but who post to every major job board.
        "jobspy-indeed": {
            "task": "app.workers.jobspy_tasks.fetch_indeed",
            "schedule": crontab(minute=12, hour="*/2"),
        },
        "jobspy-ziprecruiter": {
            "task": "app.workers.jobspy_tasks.fetch_ziprecruiter",
            "schedule": crontab(minute=22, hour="*/4"),
        },
        "jobspy-linkedin": {
            "task": "app.workers.jobspy_tasks.fetch_linkedin",
            "schedule": crontab(minute=32, hour="*/6"),
        },
        "jobspy-glassdoor": {
            "task": "app.workers.jobspy_tasks.fetch_glassdoor",
            "schedule": crontab(minute=42, hour=8),
        },

        # ── Storage / scan-pool maintenance ──────────────────────────────
        # Bronze raw-jobs TTL: drop scan payloads > 7 days every day at 04:00.
        # Bounds storage to ~7× daily scan volume; otherwise bronze grows
        # unbounded as the corpus scales.
        "prune-bronze-jobs": {
            "task": "app.workers.maintenance_tasks.prune_bronze_jobs",
            "schedule": crontab(hour=4, minute=0),
        },
        # Company decay: deactivate companies that have been dormant for
        # >180 days (no jobs ever found, or last_job_found_at too old).
        # Keeps the active scan pool tight at scale — companies that stop
        # posting jobs shouldn't burn tier-scan budget forever.  Daily at 04:15.
        "decay-inactive-companies": {
            "task": "app.workers.maintenance_tasks.decay_inactive_companies",
            "schedule": crontab(hour=4, minute=15),
        },
        # Workday board-slug fixer: hourly.  Each pass patches up to 500
        # Workday-tagged companies whose ats_identifier is missing the
        # board/shard parts.  Catches mega-employers (Tesla, Salesforce,
        # Uber, Lyft, IBM, MongoDB, Snowflake, etc.) so they actually
        # return jobs on next scan.
        "fix-workday-slugs": {
            "task": "app.workers.maintenance_tasks.fix_workday_slugs",
            "schedule": crontab(minute=20, hour="*/1"),
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
        "ai-discover-companies": {
            "task": "app.workers.ai_company_discovery.discover_via_ai",
            "schedule": crontab(minute=25, hour="*/1"),
        },
        # ATS auto-promoter — picks 50 ats=unknown companies every 10 min
        # (was every 30 min), fetches their careers page, looks for embedded
        # Greenhouse / Lever / Workday / etc. links, and auto-upgrades them.
        # Faster cadence drains the unknown backlog ~3× quicker.
        "promote-unknown-companies": {
            "task": "app.workers.ats_promoter_tasks.promote_unknown_companies",
            "schedule": crontab(minute="*/10"),
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
