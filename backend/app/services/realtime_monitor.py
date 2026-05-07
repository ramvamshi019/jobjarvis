"""
Real-time Job Monitor — sub-minute incremental ingestion.

Detects new job postings within 45–60 seconds of them appearing on ATS boards.

How it works
─────────────
1. Maintains an in-process watermark per company:
       _watermark[company_id] = set of external_ids already seen
2. Every tick (default 50 s), fetches the ATS board for each Tier-1 company.
3. Diffs the result against the watermark → only NEW external_ids are enriched
   and written.  Unchanged jobs are skipped entirely.
4. Writes new jobs via the same safe-upsert path used by the full pipeline so
   there are ZERO duplicates (ON CONFLICT DO NOTHING on company_id + external_id).

Why not re-use run_ingestion_pipeline()?
─────────────────────────────────────────
The full pipeline marks stale jobs inactive, commits per-company, and runs 500
companies per tick.  The realtime monitor is intentionally narrower:
  - Only Tier-1 companies (priority_score ≥ 80) — these are the high-signal ATS
    boards worth polling sub-minute.
  - Only inserts NEW rows — never updates existing ones.
  - Never marks anything inactive — that's the full pipeline's job.
  - Keeps its own in-memory watermark so repeated fetches are O(delta) not O(all).

Thread / task safety
─────────────────────
The monitor runs as a single asyncio.Task inside the FastAPI lifespan.
An asyncio.Semaphore(8) caps simultaneous ATS fetches.
Errors for one company never block others.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.connectors.ats import fetch_jobs_from_ats
from app.database import AsyncSessionLocal
from app.models.company import Company
from app.models.job import Job
from app.services.job_pipeline import (
    _passes_quality_gate,
    _safe_json,
    _safe_str,
    _clean_list,
)
from app.services.normalizer import (
    classify_experience_level,
    classify_role_category,
    normalize_country,
    normalize_currency,
    normalize_location,
    normalize_remote,
    normalize_title,
    parse_location,
    compute_fingerprint,        # Step 2: title+company only
    normalize_source_type,      # Step 3: canonical source_type values
    compute_data_quality_score, # Step 9: persist quality score
)
from app.ai.skill_extractor import extract_skills
from app.ai.spam_detector import detect_spam
from sqlalchemy import select, func, and_
from sqlalchemy.dialects.postgresql import insert as _db_insert

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

_TICK_SECONDS      = 50   # poll interval — between 45 and 60 s
_CONCURRENCY       = 8    # simultaneous ATS fetches per tick
_MAX_COMPANIES     = 100  # Tier-1 companies monitored per tick
_ATS_PRIORITY      = {"greenhouse", "lever", "ashby"}  # monitored first
_SKIP_AFTER_FAILS  = 5    # skip realtime scanning once consecutive_failures >= this
                          # (healer_tasks.py takes over at HEAL_THRESHOLD=3)

# ── In-process watermark ───────────────────────────────────────────────────────
# Maps company_id → frozenset of external_ids already committed to DB.
# Populated lazily on first fetch; survives across ticks.
_watermark: dict[int, set[str]] = {}
_monitor_task: asyncio.Task | None = None

# ── Metrics (readable by /admin/realtime/status) ───────────────────────────────
_stats: dict[str, Any] = {
    "running":           False,
    "ticks":             0,
    "total_new_jobs":    0,
    "last_tick_at":      None,
    "last_tick_new":     0,
    "last_tick_ms":      0,
    "companies_watched": 0,
}


# ── Watermark helpers ──────────────────────────────────────────────────────────

async def _load_watermark(company_id: int) -> set[str]:
    """Load existing external_ids for a company from DB (once per monitor start)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Job.external_id)
            .where(Job.company_id == company_id)
            .where(Job.external_id.is_not(None))
        )
        return {row[0] for row in result.all()}


# ── Per-company delta fetch ────────────────────────────────────────────────────

async def _fetch_delta(
    client: httpx.AsyncClient,
    company: Company,
    run_now: datetime,
) -> int:
    """
    Fetch jobs for one company and insert only those not in the watermark.
    Returns count of new jobs inserted.
    """
    ats_type = (company.ats_type or "").lower()
    if not ats_type or not company.ats_identifier:
        return 0

    # Lazy watermark load
    if company.id not in _watermark:
        _watermark[company.id] = await _load_watermark(company.id)

    try:
        raw_jobs = await fetch_jobs_from_ats(client, ats_type, company.ats_identifier)
    except Exception as exc:
        logger.warning(
            "realtime_fetch_error company=%s ats=%s error=%s",
            company.name, ats_type, exc,
        )
        # Increment consecutive_failures so the healer can pick this up
        # and the monitor will eventually skip it (_SKIP_AFTER_FAILS).
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Company).where(Company.id == company.id)
                )
                c = result.scalar_one_or_none()
                if c:
                    c.consecutive_failures = (c.consecutive_failures or 0) + 1
                    c.failure_count = (c.failure_count or 0) + 1
                    await db.commit()
        except Exception:
            pass
        return 0

    if not raw_jobs:
        return 0

    # Fetch succeeded — reset consecutive failures so this company stays in the
    # realtime pool (was previously above _SKIP_AFTER_FAILS and got re-healed).
    if company.consecutive_failures > 0:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Company).where(Company.id == company.id)
                )
                c = result.scalar_one_or_none()
                if c:
                    c.consecutive_failures = 0
                    await db.commit()
        except Exception:
            pass

    known = _watermark[company.id]
    new_rows: list[dict] = []
    seen_urls: set[str] = set()
    seen_fps: set[str] = set()  # Step 4: per-tick fingerprint dedup

    for r_job in raw_jobs:
        title       = normalize_title(r_job.get("title", ""))
        job_url     = r_job.get("job_url", "") or ""
        external_id = r_job.get("external_id") or ""
        description = r_job.get("description", "") or ""
        location_raw = r_job.get("location", "") or ""

        # ── Delta check — skip if already known ───────────────────────────────
        if external_id and external_id in known:
            continue

        # ── Intra-batch URL dedup ─────────────────────────────────────────────
        if job_url and job_url in seen_urls:
            continue
        if job_url:
            seen_urls.add(job_url)

        # ── Hash fallback ID ──────────────────────────────────────────────────
        if not external_id:
            import hashlib
            external_id = "rt_" + hashlib.sha256(
                f"{company.name}::{title}::{job_url}".lower().encode()
            ).hexdigest()[:16]

        if external_id in known:
            continue

        # ── Spam + quality gate ───────────────────────────────────────────────
        r_job["company_domain"] = company.domain
        spam_res = detect_spam(r_job)
        if not description and "no_description" in spam_res.spam_flags:
            spam_res.spam_score = max(0.0, spam_res.spam_score - 0.9)

        r_job["title"] = title
        passes, reason = _passes_quality_gate(r_job, spam_res.spam_score)
        if not passes:
            continue

        # ── Enrich ────────────────────────────────────────────────────────────
        location    = normalize_location(location_raw)
        city, region = parse_location(location_raw)
        country     = normalize_country(location_raw)
        remote_type = normalize_remote(location_raw, title, description)
        role_category = classify_role_category(title, description)
        experience_level = classify_experience_level(title, description)
        skills      = extract_skills(title, description)
        currency    = normalize_currency(description)
        source_conf = 0.7 if r_job.get("source") == "aggregator_api" else 1.0

        posted_at = None
        if r_job.get("posted_at"):
            try:
                posted_at = datetime.fromisoformat(
                    r_job["posted_at"].replace("Z", "+00:00")
                )
            except Exception:
                pass

        # Step 2: fingerprint = title + company_name only (cross-ATS dedup)
        fp = compute_fingerprint(title, company.name)

        # Step 4: skip if same fingerprint already queued in this tick
        if fp in seen_fps:
            logger.debug(
                "rt_batch_fp_dedup company=%s title=%r fingerprint=%s",
                company.name, title, fp[:16],
            )
            continue
        seen_fps.add(fp)

        # Step 9: compute data quality score before appending
        dqs = compute_data_quality_score(
            description, skills.required_skills, country, role_category
        )

        new_rows.append({
            "company_id":          company.id,
            "company_name":        company.name,
            "title":               title,
            "normalized_title":    title.lower(),
            "location":            location,
            "normalized_location": location.lower(),
            "city":                city,
            "region":              region,
            "country":             country if country != "unknown" else None,
            "remote_type":         remote_type if remote_type != "unknown" else None,
            # DB column is "url" — renamed here from the pipeline key "job_url"
            "url":                 job_url or None,
            "apply_url":           job_url or None,
            "external_id":         external_id,
            "source":              r_job.get("source"),
            "source_type":         normalize_source_type(
                r_job.get("source_type") or ats_type
            ),
            "source_confidence":   source_conf,
            "fingerprint":         fp,
            "salary_currency":     currency,
            "description":         description,
            "experience_level":    experience_level,
            "role_category":       role_category,
            "required_skills":     _clean_list(skills.required_skills),
            "preferred_skills":    _clean_list(skills.preferred_skills),
            "matched_tools":       _clean_list(skills.all_skills),
            "spam_score":          spam_res.spam_score,
            "data_quality_score":  dqs,
            "posted_at":           posted_at,
            "active":              True,
            "last_seen_at":        run_now,
        })

        # Optimistically update watermark — prevents double-inserts if the same
        # external_id appears across two consecutive ticks before the DB round-
        # trips.
        known.add(external_id)

    if not new_rows:
        return 0

    # ── Insert only truly new rows in chunks (DO NOTHING on conflict) ─────────
    async with AsyncSessionLocal() as db:
        from app.services.job_pipeline import BATCH_SIZE
        for i in range(0, len(new_rows), BATCH_SIZE):
            chunk = new_rows[i : i + BATCH_SIZE]
            try:
                async with db.begin_nested():
                    stmt = _db_insert(Job).values(chunk)
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=["company_id", "external_id"]
                    )
                    await db.execute(stmt)
            except Exception as exc:
                logger.error(
                    "[RT] chunk_insert_failed company=%s chunk=%d error=%s",
                    company.name, i, exc,
                )
        await db.commit()

    # Step 10: canonical summary matching scan_tasks.py / job_pipeline.py format
    total_seen = len(raw_jobs) if raw_jobs else 0
    logger.info(
        "scan_complete company=%s ats=%s "
        "checked=%d | inserted=%d | duplicates=%d | invalid=%d | duration=%.2fs",
        company.name, ats_type,
        total_seen,
        len(new_rows),
        total_seen - len(new_rows),
        0,   # realtime monitor has no separate invalid counter (quality gate drops to skipped)
        0.0, # tick duration measured at the orchestrator level
    )
    return len(new_rows)


# ── One monitor tick ───────────────────────────────────────────────────────────

async def _run_tick(
    client: httpx.AsyncClient,
    companies: list[Company],
) -> int:
    """Process all watched companies in one tick. Returns total new jobs."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    run_now = datetime.now(timezone.utc)
    total_new = 0

    async def _bounded(company: Company) -> None:
        nonlocal total_new
        async with sem:
            new = await _fetch_delta(client, company, run_now)
            total_new += new

    await asyncio.gather(*[_bounded(c) for c in companies])
    return total_new


# ── Monitor loop ───────────────────────────────────────────────────────────────

async def _monitor_loop() -> None:
    """
    Infinite loop: load Tier-1 companies, tick, sleep, repeat.
    Runs as an asyncio.Task — cancelled cleanly on shutdown.
    """
    _stats["running"] = True
    logger.info("realtime_monitor_start tick_seconds=%d", _TICK_SECONDS)

    async with httpx.AsyncClient(
        timeout=12.0,
        follow_redirects=True,
        headers={"User-Agent": "JobJarvis-RT/1.0 (+https://jobjarvis.io/bot)"},
    ) as client:
        while True:
            tick_start = time.monotonic()

            # ── Load Tier-1 companies each tick (order: ATS priority first) ──
            # Exclude companies with too many consecutive failures — the healer
            # (healer_tasks.py) handles those separately on a daily schedule.
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Company)
                    .where(Company.active == True)
                    .where(Company.ats_type.in_(list(_ATS_PRIORITY) + ["smartrecruiters", "workday"]))
                    .where(Company.priority_score >= 80)
                    .where(Company.consecutive_failures < _SKIP_AFTER_FAILS)
                    .order_by(
                        # ATS priority order: greenhouse → lever → ashby → rest
                        Company.ats_type.in_(["greenhouse"]).desc(),
                        Company.ats_type.in_(["lever"]).desc(),
                        Company.ats_type.in_(["ashby"]).desc(),
                        Company.priority_score.desc(),
                    )
                    .limit(_MAX_COMPANIES)
                )
                companies = result.scalars().all()

            _stats["companies_watched"] = len(companies)
            _stats["ticks"] += 1

            try:
                new_count = await _run_tick(client, companies)
            except Exception as exc:
                logger.error("realtime_tick_error error=%s", exc)
                new_count = 0

            elapsed_ms = int((time.monotonic() - tick_start) * 1000)
            _stats["last_tick_at"]  = datetime.now(timezone.utc).isoformat()
            _stats["last_tick_new"] = new_count
            _stats["last_tick_ms"]  = elapsed_ms
            _stats["total_new_jobs"] += new_count

            if new_count:
                logger.info(
                    "realtime_tick_complete ticks=%d new_jobs=%d elapsed_ms=%d",
                    _stats["ticks"], new_count, elapsed_ms,
                )

            # Sleep for the remainder of the tick window
            sleep_for = max(1.0, _TICK_SECONDS - (elapsed_ms / 1000))
            await asyncio.sleep(sleep_for)


# ── Public start / stop ────────────────────────────────────────────────────────

def start_realtime_monitor() -> None:
    """
    Schedule the monitor loop as an asyncio.Task.
    Call this from within the FastAPI lifespan (inside the async context).
    Safe to call multiple times — only one task runs at a time.
    """
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        logger.warning("realtime_monitor already running — ignoring duplicate start")
        return
    _monitor_task = asyncio.create_task(_monitor_loop(), name="realtime_monitor")
    logger.info("realtime_monitor_task_created")


def stop_realtime_monitor() -> None:
    """Cancel the monitor task. Call from shutdown hook."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        _stats["running"] = False
        logger.info("realtime_monitor_stopped")


def get_monitor_stats() -> dict:
    """Return a copy of live stats for the admin status endpoint."""
    return dict(_stats)
