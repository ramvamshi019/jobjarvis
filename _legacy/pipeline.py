"""
Pipeline Orchestrator — runs the full job automation workflow:
  1. Fetch jobs from all configured sources
  2. Filter, score, and extract metadata
  3. Save to database (with dedup)
  4. Generate tailored resumes + cover letters (for top matches)
  5. Create PDFs
  6. Send alerts via all configured channels
  7. Log pipeline run statistics

Features:
  - Pipeline run tracking in database
  - Progress callbacks for UI integration
  - Partial failure recovery (continues on per-job errors)
  - Structured logging with timing
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, Optional

from db import (
    init_db,
    bulk_insert_jobs,
    get_jobs_without_resume,
    get_unsent_alerts,
    update_resume_path,
    update_cover_letter,
    mark_alert_sent,
    get_job_count,
    log_pipeline_start,
    log_pipeline_end,
)
from fetch_jobs import fetch_all_jobs
from filter_jobs import filter_jobs
from generate_resume import generate_tailored_resume, generate_cover_letter
from pdf_generator import generate_pdf, generate_cover_letter_pdf
from notifier import send_job_alert, send_summary_alert, log_matches_to_console

logger = logging.getLogger(__name__)

# Type alias for optional progress callback: (step_name, detail_message)
ProgressCallback = Optional[Callable[[str, str], None]]


async def run_pipeline(
    generate_resumes: bool = True,
    send_alerts: bool = True,
    max_resumes: int = 10,
    min_score: float = 50.0,
    on_progress: ProgressCallback = None,
):
    """
    Execute the full pipeline.

    Args:
        generate_resumes: Whether to generate AI resumes for top matches.
        send_alerts: Whether to send notifications.
        max_resumes: Max number of resumes to generate per run.
        min_score: Minimum match score to generate a resume.
        on_progress: Optional callback for progress updates.
    """
    start = time.time()
    stats = {
        "new_jobs": 0,
        "total_fetched": 0,
        "resumes_generated": 0,
        "alerts_sent": 0,
        "top_score": 0,
        "total_jobs": 0,
        "duration": 0,
        "errors": [],
    }

    def _progress(step: str, msg: str):
        logger.info(f"[{step}] {msg}")
        if on_progress:
            try:
                on_progress(step, msg)
            except Exception:
                pass

    logger.info("=" * 60)
    logger.info(f"Pipeline started at {datetime.now().isoformat()}")
    logger.info("=" * 60)

    # ── Step 0: Initialize ──────────────────────────────────
    init_db()
    run_id = log_pipeline_start()

    # ── Step 1: Fetch jobs ──────────────────────────────────
    _progress("fetch", "Fetching jobs from all sources...")
    try:
        raw_jobs = await fetch_all_jobs()
        stats["total_fetched"] = len(raw_jobs)
        _progress("fetch", f"Fetched {len(raw_jobs)} raw jobs")
    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        stats["errors"].append(f"Fetch: {e}")
        raw_jobs = []

    # ── Step 2: Filter & Score ──────────────────────────────
    _progress("filter", "Filtering and scoring jobs...")
    try:
        filtered = filter_jobs(raw_jobs)
    except Exception as e:
        logger.error(f"Filter failed: {e}")
        stats["errors"].append(f"Filter: {e}")
        filtered = []
    _progress("filter", f"Filtered to {len(filtered)} relevant jobs")

    if filtered:
        stats["top_score"] = filtered[0]["match_score"]

    # ── Step 3: Save to DB ──────────────────────────────────
    _progress("save", "Saving to database...")
    inserted = bulk_insert_jobs(filtered)
    stats["new_jobs"] = inserted
    _progress("save", f"Inserted {inserted} new jobs (skipped {len(filtered) - inserted} duplicates)")

    # Console digest for quick feedback even without Telegram
    try:
        log_matches_to_console(filtered[:40], stats)
    except Exception as e:
        logger.debug("Console digest skipped: %s", e)

    # ── Step 4: Generate resumes ────────────────────────────
    if generate_resumes:
        _progress("resumes", f"Generating resumes (top {max_resumes}, score >= {min_score})...")
        jobs_needing_resume = get_jobs_without_resume(min_score)
        jobs_to_process = jobs_needing_resume[:max_resumes]

        for i, job in enumerate(jobs_to_process, 1):
            _progress("resumes", f"[{i}/{len(jobs_to_process)}] {job['company']} — {job['title']}")
            try:
                # Generate resume
                resume_text = generate_tailored_resume(
                    job["title"], job["company"], job["description"]
                )
                pdf_path = generate_pdf(resume_text, job["job_id"])
                update_resume_path(job["job_id"], pdf_path)

                # Generate cover letter
                try:
                    cl_text = generate_cover_letter(
                        job["title"], job["company"], job["description"]
                    )
                    cl_path = generate_cover_letter_pdf(cl_text, job["job_id"])
                    update_cover_letter(job["job_id"], cl_path)
                except Exception as e:
                    logger.warning(f"Cover letter failed for {job['job_id']}: {e}")

                stats["resumes_generated"] += 1
                logger.info(f"  Resume ready: {job['company']} — {job['title']}")
            except Exception as e:
                logger.error(f"  Resume failed: {job['company']} — {e}")
                stats["errors"].append(f"Resume {job['job_id']}: {e}")
    else:
        _progress("resumes", "Skipping resume generation")

    # ── Step 5: Send alerts ─────────────────────────────────
    if send_alerts:
        _progress("alerts", "Sending alerts...")
        unsent = get_unsent_alerts()
        alert_batch = unsent[:20]  # Cap alerts per run

        for job in alert_batch:
            try:
                success = await send_job_alert(job)
                if success:
                    mark_alert_sent(job["job_id"])
                    stats["alerts_sent"] += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Alert failed for {job['job_id']}: {e}")

        _progress("alerts", f"Sent {stats['alerts_sent']}/{len(alert_batch)} alerts")
    else:
        _progress("alerts", "Skipping alerts")

    # ── Summary ─────────────────────────────────────────────
    counts = get_job_count()
    stats["total_jobs"] = counts["total"]
    stats["duration"] = round(time.time() - start, 1)

    logger.info("\n" + "=" * 60)
    logger.info(f"Pipeline complete in {stats['duration']}s")
    logger.info(f"  Fetched:           {stats['total_fetched']}")
    logger.info(f"  New jobs:          {stats['new_jobs']}")
    logger.info(f"  Resumes generated: {stats['resumes_generated']}")
    logger.info(f"  Alerts sent:       {stats['alerts_sent']}")
    logger.info(f"  Top match score:   {stats['top_score']}")
    logger.info(f"  Total in DB:       {stats['total_jobs']}")
    if stats["errors"]:
        logger.warning(f"  Errors:            {len(stats['errors'])}")
    logger.info("=" * 60)

    # Log pipeline run to DB
    try:
        log_pipeline_end(run_id, stats)
    except Exception as e:
        logger.warning(f"Failed to log pipeline end: {e}")

    # Send summary alert
    if send_alerts:
        try:
            await send_summary_alert(stats)
        except Exception:
            pass

    _progress("done", "Pipeline complete!")
    return stats


def run_pipeline_sync(**kwargs):
    """Synchronous wrapper for use from Streamlit or CLI."""
    return asyncio.run(run_pipeline(**kwargs))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run_pipeline_sync(generate_resumes=False, send_alerts=False)
