"""Celery task that runs auto-apply in the worker container (has Playwright)."""
import os
import asyncio
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.auto_apply_tasks.run_one",
                 soft_time_limit=300, max_retries=0)
def run_one(*, user_id, user_email, user_name, resume_path, resume_text,
            job_id, job_url, company, title, dry_run=True, profile=None):
    """
    Run auto-apply for one job inside the worker container, where Playwright
    + Chromium are installed and PLAYWRIGHT_BROWSERS_PATH is set.
    """
    # Ensure Playwright finds its browsers dir
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/pw_browsers")

    from app.services.auto_apply import apply_to_job
    return asyncio.run(apply_to_job(
        user_id=user_id, user_email=user_email, user_name=user_name,
        resume_path=resume_path, resume_text=resume_text,
        job_id=job_id, job_url=job_url, company=company, title=title,
        dry_run=dry_run, profile=profile,
    ))
