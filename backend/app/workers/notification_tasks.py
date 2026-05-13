"""Celery task that pushes new-match notifications every 15 minutes."""
import asyncio
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.notification_tasks.push_new_match_notifications",
                 soft_time_limit=300, max_retries=1)
def push_new_match_notifications():
    """Every 15 minutes: send Slack/email to each user about new top matches."""
    from app.services.notifications import notify_all_users
    return asyncio.run(notify_all_users())
