"""
Production Scheduler — runs the pipeline on a configurable interval.

Features:
  - Graceful shutdown via SIGINT/SIGTERM
  - Lock file to prevent overlapping runs
  - Structured logging with file rotation
  - Health check file (touch-based, for external monitors)
  - Dry-run mode for testing configuration
  - Configurable retry on pipeline failure

Usage:
    python scheduler.py                          # Run once (fetch only)
    python scheduler.py --loop                   # Run every 30 minutes
    python scheduler.py --loop --interval 15     # Run every 15 minutes
    python scheduler.py --resumes --alerts       # Generate resumes + send alerts
    python scheduler.py --dry-run                # Validate config without running
"""

import argparse
import atexit
import logging
import logging.handlers
import os
import sys
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

from pipeline import run_pipeline_sync
from config import validate_config, DB_PATH

# ─── Constants ─────────────────────────────────────────────────
DATA_DIR = Path("data")
LOG_FILE = DATA_DIR / "pipeline.log"
LOCK_FILE = DATA_DIR / "scheduler.lock"
HEALTH_FILE = DATA_DIR / "scheduler.health"
PID_FILE = DATA_DIR / "scheduler.pid"

MAX_CONSECUTIVE_FAILURES = 5


# ─── Logging Setup ─────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> None:
    """Configure structured logging with rotation."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO

    # Console handler — concise
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    # File handler — detailed, with rotation (5 MB, keep 3 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    logging.basicConfig(level=logging.DEBUG, handlers=[console, file_handler])


logger = logging.getLogger("scheduler")


# ─── Lock File (prevent overlapping runs) ──────────────────────

def acquire_lock() -> bool:
    """Create a lock file with the current PID. Returns False if already locked."""
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            # Check if the old process is still running
            try:
                os.kill(old_pid, 0)
                logger.error(f"Another scheduler is running (PID {old_pid}). Exiting.")
                return False
            except OSError:
                logger.warning(f"Stale lock file found (PID {old_pid} is dead). Removing.")
                LOCK_FILE.unlink(missing_ok=True)
        except (ValueError, FileNotFoundError):
            LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    """Remove the lock file."""
    LOCK_FILE.unlink(missing_ok=True)


def touch_health() -> None:
    """Update the health file timestamp (for external monitoring)."""
    try:
        HEALTH_FILE.write_text(datetime.now().isoformat())
    except Exception:
        pass


# ─── Graceful Shutdown ─────────────────────────────────────────

_running = True


def _shutdown_handler(sig, frame):
    global _running
    sig_name = signal.Signals(sig).name
    logger.info(f"Received {sig_name}. Finishing current run before shutdown...")
    _running = False


signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)


# ─── Main ──────────────────────────────────────────────────────

def run_once(
    generate_resumes: bool = False,
    send_alerts: bool = False,
    max_resumes: int = 10,
    min_score: float = 50.0,
) -> dict:
    """Execute a single pipeline run and return stats."""
    start = datetime.now()
    logger.info("=" * 60)
    logger.info(f"Pipeline run started at {start.isoformat()}")

    stats = run_pipeline_sync(
        generate_resumes=generate_resumes,
        send_alerts=send_alerts,
        max_resumes=max_resumes,
        min_score=min_score,
    )

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(
        f"Pipeline complete in {elapsed:.1f}s — "
        f"fetched={stats.get('total_fetched', 0)}, "
        f"new={stats.get('new_jobs', 0)}, "
        f"resumes={stats.get('resumes_generated', 0)}, "
        f"alerts={stats.get('alerts_sent', 0)}"
    )

    touch_health()
    return stats


def run_loop(
    interval_minutes: int = 30,
    generate_resumes: bool = False,
    send_alerts: bool = False,
    max_resumes: int = 10,
    min_score: float = 50.0,
) -> None:
    """Run the pipeline on a repeating schedule."""
    global _running
    consecutive_failures = 0
    run_count = 0

    logger.info(f"Scheduler loop started (interval={interval_minutes}min)")

    while _running:
        run_count += 1
        logger.info(f"--- Run #{run_count} ---")

        try:
            stats = run_once(
                generate_resumes=generate_resumes,
                send_alerts=send_alerts,
                max_resumes=max_resumes,
                min_score=min_score,
            )
            errors = stats.get("errors", [])
            if errors:
                logger.warning(f"Run completed with {len(errors)} error(s)")
                consecutive_failures += 1
            else:
                consecutive_failures = 0

        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Pipeline run failed: {e}", exc_info=True)

        # Circuit breaker: back off if too many failures
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            backoff = min(consecutive_failures * interval_minutes, 180)
            logger.error(
                f"{consecutive_failures} consecutive failures. "
                f"Backing off for {backoff} minutes."
            )
            _sleep_interruptible(backoff * 60)
            consecutive_failures = 0
            continue

        if not _running:
            break

        next_run = datetime.now() + timedelta(minutes=interval_minutes)
        logger.info(f"Next run at {next_run.strftime('%H:%M:%S')} ({interval_minutes}min)")
        _sleep_interruptible(interval_minutes * 60)

    logger.info(f"Scheduler stopped after {run_count} run(s).")


def _sleep_interruptible(seconds: int) -> None:
    """Sleep in 1-second increments so we can respond to shutdown signals."""
    for _ in range(int(seconds)):
        if not _running:
            return
        time.sleep(1)


def dry_run() -> None:
    """Validate configuration without running the pipeline."""
    logger.info("=" * 60)
    logger.info("DRY RUN — validating configuration")
    logger.info("=" * 60)

    warnings = validate_config()

    # Check data directory
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Data directory: {DATA_DIR.resolve()} (OK)")

    # Check DB path
    db_dir = Path(DB_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Database path: {DB_PATH} (directory exists)")

    if warnings:
        logger.warning(f"{len(warnings)} configuration warning(s):")
        for w in warnings:
            logger.warning(f"  - {w}")
    else:
        logger.info("All configuration checks passed!")

    logger.info("Dry run complete. System is ready.")


def main():
    parser = argparse.ArgumentParser(
        description="Job Automation Scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scheduler.py                          # Single run, fetch only
  python scheduler.py --loop                   # Loop every 30 min
  python scheduler.py --loop --interval 15     # Loop every 15 min
  python scheduler.py --resumes --alerts       # Fetch + resumes + alerts
  python scheduler.py --dry-run                # Config check only
  python scheduler.py --loop -v                # Verbose debug logging
        """,
    )

    parser.add_argument("--loop", action="store_true", help="Run continuously on a schedule")
    parser.add_argument("--interval", type=int, default=30, help="Minutes between runs (default: 30)")
    parser.add_argument("--resumes", action="store_true", help="Generate tailored resumes for top matches")
    parser.add_argument("--alerts", action="store_true", help="Send notifications (Telegram/email)")
    parser.add_argument("--max-resumes", type=int, default=10, help="Max resumes per run (default: 10)")
    parser.add_argument("--min-score", type=float, default=50.0, help="Min match score for resume generation (default: 50)")
    parser.add_argument("--dry-run", action="store_true", help="Validate config without running pipeline")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    # Dry run mode
    if args.dry_run:
        dry_run()
        return

    # Acquire lock
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_lock():
        sys.exit(1)
    atexit.register(release_lock)

    logger.info("Job Automation Scheduler v2.0")
    logger.info(
        f"  Mode: {'loop' if args.loop else 'single'} | "
        f"Interval: {args.interval}min | "
        f"Resumes: {args.resumes} | "
        f"Alerts: {args.alerts}"
    )

    try:
        if args.loop:
            run_loop(
                interval_minutes=args.interval,
                generate_resumes=args.resumes,
                send_alerts=args.alerts,
                max_resumes=args.max_resumes,
                min_score=args.min_score,
            )
        else:
            run_once(
                generate_resumes=args.resumes,
                send_alerts=args.alerts,
                max_resumes=args.max_resumes,
                min_score=args.min_score,
            )
    except Exception as e:
        logger.critical(f"Unhandled error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        release_lock()
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
