"""
Browser-extension endpoints.

POST /api/extension/save_url
    body: {"url": "https://boards.greenhouse.io/acme/jobs/123"}
    Detects ATS, adds the company if missing, queues a scan.
    Returns:  {company_id, ats, slug, jobs_known, action: "added|exists"}
"""
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/extension", tags=["browser-extension"])


# Same ATS classifier we use elsewhere
_ATS_PATTERNS = [
    (re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9][\w-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9][\w-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][\w-]+)", re.I), "ashby"),
    (re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9][\w-]+)", re.I), "smartrecruiters"),
    (re.compile(r"apply\.workable\.com/([a-z0-9][\w-]+)", re.I), "workable"),
    (re.compile(r"([a-z0-9][\w-]+)\.workable\.com", re.I), "workable"),
    (re.compile(r"([a-z0-9][\w-]+)\.bamboohr\.com", re.I), "bamboohr"),
    (re.compile(r"([a-z0-9][\w-]+)\.recruitee\.com", re.I), "recruitee"),
    (re.compile(r"careers-([a-z0-9][\w-]+)\.icims\.com", re.I), "icims"),
    (re.compile(r"([a-z0-9][\w-]+)\.icims\.com", re.I), "icims"),
    (re.compile(r"([a-z0-9][\w-]+)\.(?:wd[0-9]+\.)?myworkdayjobs\.com", re.I), "workday"),
    (re.compile(r"([a-z0-9][\w-]+)\.teamtailor\.com", re.I), "teamtailor"),
    (re.compile(r"jobs\.jobvite\.com/([a-z0-9][\w-]+)", re.I), "jobvite"),
]


def _detect(url: str):
    for pat, ats in _ATS_PATTERNS:
        m = pat.search(url)
        if m:
            slug = m.group(1).strip().lower()
            if slug not in {"www", "jobs", "careers", "embed", "api", "support"}:
                return ats, slug
    return None


def _slug_to_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", slug) if w)


class SaveUrlRequest(BaseModel):
    url: str


class SaveUrlResponse(BaseModel):
    ok: bool
    action: str            # "added" | "exists" | "unsupported_ats"
    ats: str | None = None
    slug: str | None = None
    company_id: int | None = None
    message: str


@router.post("/save_url", response_model=SaveUrlResponse)
async def save_url(
    req: SaveUrlRequest,
    db: AsyncSession = Depends(get_db),
):
    """Auto-add a company from any pasted career-page URL."""
    hit = _detect(req.url)
    if not hit:
        return SaveUrlResponse(
            ok=False, action="unsupported_ats",
            message="That URL isn't from a supported ATS yet "
                    "(Greenhouse, Lever, Ashby, SmartRecruiters, Workable, "
                    "BambooHR, Recruitee, iCIMS, Workday, TeamTailor, Jobvite).",
        )
    ats, slug = hit

    # Check if exists
    existing = await db.execute(
        text("SELECT id FROM companies WHERE ats=:ats AND ats_identifier=:slug LIMIT 1"),
        {"ats": ats, "slug": slug},
    )
    row = existing.first()
    if row:
        return SaveUrlResponse(
            ok=True, action="exists", ats=ats, slug=slug,
            company_id=row[0],
            message=f"{_slug_to_name(slug)} is already tracked.",
        )

    # Insert
    now = datetime.now(timezone.utc)
    soon = now + timedelta(minutes=2)  # scan this one right away
    name = _slug_to_name(slug)
    careers_url = req.url[:5000]

    inserted = await db.execute(
        text(
            """
            INSERT INTO companies (
                name, ats, ats_identifier, careers_url,
                priority_score, scan_priority,
                scan_frequency_minutes, next_scan_at, active,
                failure_count, consecutive_failures, jobs_found_count,
                is_blocklisted, robots_txt_compliant,
                created_at, updated_at
            ) VALUES (
                :name, :ats, :slug, :url,
                70, 70, 60, :soon, true,
                0, 0, 0, false, true, :now, :now
            )
            ON CONFLICT (name) DO UPDATE SET
                ats = EXCLUDED.ats,
                ats_identifier = EXCLUDED.ats_identifier,
                active = true,
                updated_at = EXCLUDED.updated_at
            RETURNING id
            """
        ),
        {"name": name, "ats": ats, "slug": slug, "url": careers_url,
         "soon": soon, "now": now},
    )
    new_id = inserted.scalar()
    await db.commit()

    return SaveUrlResponse(
        ok=True, action="added", ats=ats, slug=slug, company_id=new_id,
        message=f"Added {name} ({ats}). Scanner will fetch their jobs in ~2 min.",
    )
