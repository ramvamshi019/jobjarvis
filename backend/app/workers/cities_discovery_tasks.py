"""
Celery wrapper around scripts/discover_us_cities.py — scrapes 30 major US
tech metros (Wikidata + Built In) and upserts every discovered company
into the companies table.

Scheduled weekly by celery beat (see celery_app.py beat_schedule).
Cities don't change that often; weekly is enough.
"""
import asyncio
import subprocess

import structlog

from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(
    name="app.workers.cities_discovery_tasks.discover_us_cities",
    soft_time_limit=3600,
    max_retries=1,
)
def discover_us_cities():
    """
    Run the 30-city discovery script as a subprocess.  Subprocess isolation
    keeps the script's argparse / asyncio loop from clashing with Celery's
    event loop, and lets us reuse the exact same script we run by hand.
    """
    try:
        proc = subprocess.run(
            ["python3", "-u", "/app/scripts/discover_us_cities.py",
             "--out", "/tmp/us_cities_scrape.json"],
            capture_output=True,
            text=True,
            timeout=3500,
        )
        logger.info(
            "us_cities_discovery_done",
            returncode=proc.returncode,
            stdout_tail=proc.stdout[-1000:],
            stderr_tail=proc.stderr[-500:],
        )
        return {
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as e:
        logger.error("us_cities_discovery_timeout", error=str(e))
        return {"returncode": -1, "ok": False, "error": "timeout"}
    except Exception as e:
        logger.exception("us_cities_discovery_failed", error=str(e))
        return {"returncode": -1, "ok": False, "error": str(e)}


@celery_app.task(
    name="app.workers.cities_discovery_tasks.discover_one_city",
    soft_time_limit=600,
)
def discover_one_city(city_key: str):
    """Test helper: run discovery for one city only."""
    try:
        proc = subprocess.run(
            ["python3", "-u", "/app/scripts/discover_us_cities.py",
             "--city", city_key,
             "--out", f"/tmp/us_cities_scrape_{city_key}.json"],
            capture_output=True,
            text=True,
            timeout=580,
        )
        logger.info(
            "discover_one_city_done",
            city=city_key,
            returncode=proc.returncode,
            stdout_tail=proc.stdout[-2000:],
        )
        return {"city": city_key, "returncode": proc.returncode}
    except Exception as e:
        logger.exception("discover_one_city_failed", city=city_key, error=str(e))
        return {"city": city_key, "error": str(e)}
