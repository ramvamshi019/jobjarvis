"""
Bulk company-discovery Celery tasks.

Wraps the four standalone scripts in backend/scripts/ that historically had
to be run manually via `docker exec`:

  * discover_hn_whoshiring.py   — HN "Ask HN: Who's hiring" threads     (~3-5k)
  * discover_yc_complete.py     — every YC batch directory page         (~5k)
  * discover_awesome_lists.py   — github awesome-* + curated repos      (~2-3k)
  * discover_known_lists.py     — Common Crawl CDX over known ATS hosts (~5-15k)

Each runs on a weekly cadence with HN daily, and the `bootstrap_all_sources`
task fires all four sequentially for first-time setup.  All tasks idempotently
upsert into the `companies` table via discovery_lib.upsert_company, so re-running
is safe — duplicates collapse on the case-insensitive name unique index.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import structlog

from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

# The scripts live in /app/scripts in the prod image (COPY . . in Dockerfile)
# but during local pytest the repo lives at backend/ — handle both.
_SCRIPTS_DIR = Path(os.environ.get("DISCOVERY_SCRIPTS_DIR", "/app/scripts"))
if not _SCRIPTS_DIR.exists():
    _here = Path(__file__).resolve()
    _candidate = _here.parents[2] / "scripts"
    if _candidate.exists():
        _SCRIPTS_DIR = _candidate

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _import_script(module_name: str):
    """Import a script from _SCRIPTS_DIR by filename, returning the module."""
    path = _SCRIPTS_DIR / f"{module_name}.py"
    if not path.exists():
        raise FileNotFoundError(f"Discovery script not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_async(coro):
    """Open a fresh event loop per task (Celery workers fork)."""
    return asyncio.run(coro)


# ── Individual source wrappers ────────────────────────────────────────────────

@celery_app.task(
    name="app.workers.bulk_discovery_tasks.discover_hn_whoshiring",
    soft_time_limit=1800,  # 30 min — HN crawl can take a while
    max_retries=1,
)
def discover_hn_whoshiring_task() -> dict:
    """Crawl every monthly 'Ask HN: Who is hiring' thread and upsert each
    company whose careers URL we can classify.  ~3-5k new companies per run.
    """
    logger.info("bulk_disco_hn_start")
    try:
        mod = _import_script("discover_hn_whoshiring")
        _run_async(mod.main())
        logger.info("bulk_disco_hn_done")
        return {"source": "hn_whoshiring", "ok": True}
    except Exception as exc:
        logger.exception("bulk_disco_hn_failed", err=str(exc))
        return {"source": "hn_whoshiring", "ok": False, "error": str(exc)}


@celery_app.task(
    name="app.workers.bulk_discovery_tasks.discover_yc_complete",
    soft_time_limit=1200,
    max_retries=1,
)
def discover_yc_complete_task() -> dict:
    """Pull every YC batch directory page and upsert each company."""
    logger.info("bulk_disco_yc_start")
    try:
        mod = _import_script("discover_yc_complete")
        _run_async(mod.main())
        logger.info("bulk_disco_yc_done")
        return {"source": "yc_complete", "ok": True}
    except Exception as exc:
        logger.exception("bulk_disco_yc_failed", err=str(exc))
        return {"source": "yc_complete", "ok": False, "error": str(exc)}


@celery_app.task(
    name="app.workers.bulk_discovery_tasks.discover_awesome_lists",
    soft_time_limit=900,
    max_retries=1,
)
def discover_awesome_lists_task() -> dict:
    """Scrape curated github awesome-* READMEs for tech-company careers links."""
    logger.info("bulk_disco_awesome_start")
    try:
        mod = _import_script("discover_awesome_lists")
        _run_async(mod.main())
        logger.info("bulk_disco_awesome_done")
        return {"source": "awesome_lists", "ok": True}
    except Exception as exc:
        logger.exception("bulk_disco_awesome_failed", err=str(exc))
        return {"source": "awesome_lists", "ok": False, "error": str(exc)}


@celery_app.task(
    name="app.workers.bulk_discovery_tasks.discover_known_lists",
    soft_time_limit=2400,  # Common Crawl can be slow
    max_retries=1,
)
def discover_known_lists_task() -> dict:
    """Query Common Crawl's CDX index for every URL matching a known ATS host
    (boards.greenhouse.io, jobs.lever.co, *.workable.com, etc.) and upsert
    each unique company.  This is the heaviest source: 5-15k new companies."""
    logger.info("bulk_disco_cc_start")
    try:
        mod = _import_script("discover_known_lists")
        _run_async(mod.main())
        logger.info("bulk_disco_cc_done")
        return {"source": "common_crawl", "ok": True}
    except Exception as exc:
        logger.exception("bulk_disco_cc_failed", err=str(exc))
        return {"source": "common_crawl", "ok": False, "error": str(exc)}


# ── Sequential bootstrap (manual trigger) ─────────────────────────────────────

@celery_app.task(
    name="app.workers.bulk_discovery_tasks.bootstrap_all_sources",
    soft_time_limit=7200,  # 2 h — full bootstrap can take ~30-45 min
    max_retries=0,
)
def bootstrap_all_sources_task() -> dict:
    """Fire all four bulk sources back-to-back.  Designed to be called once
    from an admin endpoint when the corpus is small — adds ~10-25k US tech
    companies in a single ~30-45 minute run.

    Idempotent: each source dedups on insert.  Safe to re-run.
    """
    results: list[dict] = []
    for fn in (
        discover_hn_whoshiring_task,
        discover_yc_complete_task,
        discover_awesome_lists_task,
        discover_known_lists_task,
    ):
        # Call the underlying Python function synchronously (not via Celery)
        # so the bootstrap task itself stays the single Celery unit of work.
        try:
            r = fn.run()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.exception("bootstrap_step_failed", step=fn.name, err=str(exc))
            r = {"source": fn.name, "ok": False, "error": str(exc)}
        results.append(r)

    n_ok = sum(1 for r in results if r.get("ok"))
    logger.info("bootstrap_all_sources_done", succeeded=n_ok, total=len(results))
    return {"results": results, "succeeded": n_ok, "total": len(results)}
