"""Personalized job matches — driven by resume embedding cosine similarity."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.services.match_service import (
    list_matches_for_user,
    recompute_matches_for_user,
)

router = APIRouter(prefix="/matches", tags=["matches"])


def _serialize(m: dict) -> dict:
    """Convert raw row to JSON-friendly dict."""
    return {
        "job_id":          m["id"],
        "title":           m["title"],
        "company":         m.get("company_name") or "",
        "location":        m.get("location"),
        "remote_type":     m.get("remote_type"),
        "salary_min":      m.get("salary_min"),
        "salary_max":      m.get("salary_max"),
        "url":             m.get("url"),
        "apply_url":       m.get("apply_url") or m.get("url"),
        "posted_at":       m["posted_at"].isoformat()    if m.get("posted_at")    else None,
        "first_seen_at":   m["first_seen_at"].isoformat() if m.get("first_seen_at") else None,
        "source_ats":      m.get("source_ats"),
        "match_score":     float(m["match_score"]),
        "sim_score":       float(m["sim_score"])      if m.get("sim_score")      is not None else None,
        "salary_fit":      float(m["salary_fit"])     if m.get("salary_fit")     is not None else None,
        "location_fit":    float(m["location_fit"])   if m.get("location_fit")   is not None else None,
        "freshness_score": float(m["freshness_score"]) if m.get("freshness_score") is not None else None,
    }


@router.get("")
async def get_matches(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return the user's persisted top matches (sorted by composite score).
    The matches are populated automatically on resume upload; the user can
    also force a recompute via POST /matches/refresh.
    """
    rows = await list_matches_for_user(db, current_user.id, limit=limit)
    return {
        "user_id": current_user.id,
        "count": len(rows),
        "matches": [_serialize(r) for r in rows],
    }


@router.post("/refresh")
async def refresh_matches(
    top: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recompute matches for the current user (uses active resume)."""
    n = await recompute_matches_for_user(db, current_user.id, top=top)
    return {"computed": n}
