"""Freshness labels for jobs based on first_seen_at."""
from datetime import datetime, timezone, timedelta
from typing import Optional


FRESHNESS_LABELS = {
    "new_last_hour": timedelta(hours=1),
    "new_last_6_hours": timedelta(hours=6),
    "new_today": timedelta(hours=24),
    "new_last_3_days": timedelta(days=3),
}


def compute_freshness(first_seen_at: Optional[datetime], last_seen_at: Optional[datetime] = None) -> str:
    if not first_seen_at:
        return "active_unknown"

    now = datetime.now(timezone.utc)
    if first_seen_at.tzinfo is None:
        first_seen_at = first_seen_at.replace(tzinfo=timezone.utc)

    age = now - first_seen_at

    # Detect repost: job disappeared and came back
    if last_seen_at and first_seen_at:
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)

    for label, threshold in FRESHNESS_LABELS.items():
        if age <= threshold:
            return label

    if age <= timedelta(days=14):
        return "stale"

    return "stale"


def is_fresh_enough_for_ai(first_seen_at: Optional[datetime], max_age_hours: int = 72) -> bool:
    """Only run expensive AI on reasonably fresh jobs."""
    if not first_seen_at:
        return False
    now = datetime.now(timezone.utc)
    if first_seen_at.tzinfo is None:
        first_seen_at = first_seen_at.replace(tzinfo=timezone.utc)
    return (now - first_seen_at) <= timedelta(hours=max_age_hours)
