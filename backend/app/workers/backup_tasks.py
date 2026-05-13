"""
Daily Postgres backup task.  Dumps the jobjarvis DB into a timestamped
.sql.gz file under /app/backups (which should be a mounted persistent
volume in production).

Retention: keeps the 7 most recent backups, deletes older ones.

Manual run:
  docker exec jobjarvis_celery_worker python3 -c "
  from app.workers.backup_tasks import backup_database; print(backup_database())"

Scheduled by celery_app.py beat_schedule: daily 03:30 UTC.
"""
import gzip
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/backups"))
MAX_BACKUPS_KEPT = int(os.environ.get("BACKUP_RETENTION", "7"))


def _parse_db_url() -> dict:
    """Pull connection params from DATABASE_URL."""
    from urllib.parse import urlparse
    raw = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    u = urlparse(raw)
    return {
        "host": u.hostname or "postgres",
        "port": str(u.port or 5432),
        "user": u.username or "jobjarvis",
        "password": u.password or "",
        "db":   (u.path or "/jobjarvis").lstrip("/"),
    }


@celery_app.task(
    name="app.workers.backup_tasks.backup_database",
    soft_time_limit=1800,
    max_retries=1,
)
def backup_database() -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    p = _parse_db_url()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = BACKUP_DIR / f"jobjarvis_{ts}.sql.gz"

    t0 = time.time()
    env = os.environ.copy()
    env["PGPASSWORD"] = p["password"]

    # Stream pg_dump → gzip to the file (avoids holding the full dump in RAM)
    try:
        with gzip.open(out_path, "wb") as gz:
            dump = subprocess.Popen(
                [
                    "pg_dump",
                    "-h", p["host"], "-p", p["port"],
                    "-U", p["user"], "-d", p["db"],
                    "--no-owner", "--no-acl",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            shutil.copyfileobj(dump.stdout, gz)
            dump.wait(timeout=1700)
            if dump.returncode != 0:
                err = dump.stderr.read().decode("utf-8", errors="ignore")
                raise RuntimeError(f"pg_dump failed: {err[:500]}")
    except Exception as e:
        logger.exception("backup_failed", error=str(e))
        try: out_path.unlink(missing_ok=True)
        except Exception: pass
        return {"ok": False, "error": str(e)}

    size_mb = out_path.stat().st_size / 1_048_576
    elapsed = round(time.time() - t0, 1)
    logger.info(
        "backup_complete", file=str(out_path), size_mb=round(size_mb, 1),
        elapsed_s=elapsed,
    )

    # Retention: prune old backups
    backups = sorted(BACKUP_DIR.glob("jobjarvis_*.sql.gz"))
    removed = 0
    while len(backups) > MAX_BACKUPS_KEPT:
        old = backups.pop(0)
        try:
            old.unlink()
            removed += 1
        except Exception:
            pass

    return {
        "ok": True,
        "file": str(out_path),
        "size_mb": round(size_mb, 1),
        "elapsed_s": elapsed,
        "old_removed": removed,
        "total_kept": len(list(BACKUP_DIR.glob("jobjarvis_*.sql.gz"))),
    }
