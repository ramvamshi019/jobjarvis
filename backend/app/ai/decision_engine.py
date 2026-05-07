"""
Decision engine — calculate fit scores and produce APPLY_NOW / TAILOR_RESUME_FIRST / SKIP decisions.

Scoring model (100 pts total, can go negative before cap):
  1. Title/Role match       — up to 30 pts
  2. Skill match            — up to 30 pts  ← PRIMARY signal
  3. Experience match       — up to 20 pts
  4. Location match         — up to 10 pts
  5. Recency                — -15 to +10 pts
  6. Company signal         — up to 10 pts

Data-quality penalties:
  - No skills in DB         → skill_match = 0, score -= 10, confidence *= 0.6
  - No description          → confidence *= 0.6
  - Thin data (both above)  → final score hard-capped at 50, decision forced REVIEW

WHY:
  Without these penalties a job with no skill data scores 90+ from title match
  + perfect experience + fresh posting alone. That produces false APPLY decisions
  and trains users to distrust the engine. Penalising missing data ensures the
  score reflects genuine signal rather than absence of contradictory evidence.
"""
import datetime
from typing import Optional, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.job import Job
from app.models.user import User
from app.models.resume import ResumeVersion
from app.models.ai_models import AIDecision, DecisionType


# Skills that incur a heavier penalty when missing
_CORE_TECH: frozenset[str] = frozenset({
    "python", "sql", "react", "node", "node.js", "java", "aws", "docker",
    "kubernetes", "typescript", "c++", "go", "spark", "pyspark", "tensorflow",
    "pytorch", "scala",
})


def calculate_fit_score(
    job: Job,
    user: User,
    active_resume: Optional[ResumeVersion],
    user_stats: Dict[str, Any] | None = None,
    strategy: str = "balanced",
) -> Tuple[float, Dict[str, Any]]:
    """
    Return (capped_score_0_to_100, insights_dict).
    Intermediate score can go negative (pulls final cap down).
    """
    user_stats = user_stats or {}
    score = 0.0
    insights: Dict[str, Any] = {
        "title_match": 0.0,
        "skill_match": 0.0,
        "experience_match": 0.0,
        "location_match": 0.0,
        "recency_match": 0.0,
        "company_signal": 0.0,
        "missing_skills": [],
        "why_apply": [],
        "why_not": [],
        "confidence": 0.0,
        "apply_within_hours": 48,
        "interview_probability": 0.0,
        "missing_core": 0,
        "competitiveness": "Unknown",
        "roi_score": 0.0,
        "application_strategy": "",
        "data_quality": "full",
        "data_quality_score": 0.0,
    }

    # ── 0. Data quality assessment ─────────────────────────────────────────────
    description = job.description or ""
    has_description = len(description.strip()) >= 80
    has_skills_in_db = bool(job.required_skills or job.preferred_skills)

    missing_flags = (not has_description, not has_skills_in_db)
    if all(missing_flags):
        data_quality = "thin"
    elif any(missing_flags):
        data_quality = "partial"
    else:
        data_quality = "full"
    insights["data_quality"] = data_quality

    # Numeric 0–1 quality score: 0.25 per signal present.
    # Matches the pipeline's _data_quality_score() logic for consistency.
    _dqs = 0.0
    if has_skills_in_db:                                          _dqs += 0.25
    if has_description:                                           _dqs += 0.25
    if job.country and job.country not in ("unknown", "", None): _dqs += 0.25
    if job.role_category and job.role_category != "Other":        _dqs += 0.25
    insights["data_quality_score"] = round(_dqs, 2)

    # confidence_multiplier: starts at 1.0, reduced per data gap.
    # We accumulate multipliers and apply them once at the end of section 7.
    # Using a multiplier (not hard cap) avoids over-penalising; a job that is
    # still strong on title + experience should remain scoreable, just with
    # reduced certainty.
    confidence_mult = 1.0
    if not has_description:
        confidence_mult *= 0.6
    elif len(description) < 200:
        confidence_mult *= 0.8

    # ── 1. Title / Role match (30 pts) ────────────────────────────────────────
    user_roles = list(user.target_roles or [])
    if active_resume and active_resume.target_role:
        user_roles.append(active_resume.target_role)
    user_roles_lower = [r.lower() for r in user_roles]

    title_lower     = (job.title or "").lower()
    role_cat_lower  = (job.role_category or "").lower()

    if not user_roles_lower:
        title_match = 15.0  # no preference → neutral
    else:
        matched_role = next(
            (r for r in user_roles_lower if r in title_lower or r in role_cat_lower),
            None,
        )
        if matched_role:
            title_match = 30.0
            insights["why_apply"].append(f"Title matches target role: {matched_role.title()}")
        else:
            title_match = 5.0
            insights["why_not"].append("Role misalignment: category doesn't match your goals.")
            score -= 10.0

    if user_stats.get("successful_roles") and role_cat_lower in user_stats["successful_roles"]:
        score += 8.0
        insights["why_apply"].append("Historical data: strong success rate in this role.")

    score += title_match
    insights["title_match"] = title_match

    # ── 1b. Role-category mismatch penalty (−5) ───────────────────────────────
    # Applies when role_category is a concrete, known category that provably does
    # not overlap with any of the user's target roles.  This is a light additional
    # penalty on top of the title-mismatch −10 above (both can fire together).
    # Skipped when role_category is "Other" / empty — those are ambiguous, not
    # definitively wrong.
    if (
        user_roles_lower
        and job.role_category
        and job.role_category.lower() not in ("other", "unknown", "")
        and not any(
            job.role_category.lower() in r or r in job.role_category.lower()
            for r in user_roles_lower
        )
    ):
        score -= 5.0
        insights["why_not"].append(
            f"Role category '{job.role_category}' is outside your target roles."
        )

    # ── 2. Skill match (30 pts) — PRIMARY signal ──────────────────────────────
    job_skills: list[str] = job.required_skills or []
    if isinstance(job_skills, dict):
        job_skills = list(job_skills.keys())

    user_skills: list[str] = []
    if active_resume:
        sk = active_resume.skills_json
        if isinstance(sk, dict):
            user_skills = list(sk.keys())
        elif isinstance(sk, list):
            user_skills = list(sk)

    user_skills_lower = {s.lower() for s in user_skills}
    job_skills_lower  = [s.lower() for s in job_skills]

    skill_match = 0.0

    if not job_skills_lower:
        # No skill data in DB: zero skill score + moderate penalty + confidence hit.
        # -10 (not -20) keeps the penalty proportionate — the job might still be
        # genuinely relevant, we just have incomplete data to confirm it.
        skill_match = 0.0
        score -= 10.0
        confidence_mult *= 0.6   # compound with any existing reduction
        insights["why_not"].append(
            "No skill data extracted for this job — score penalised (-10)."
        )
    elif not user_skills_lower:
        skill_match = 10.0
        confidence_mult *= 0.8
        insights["why_not"].append(
            "No resume skills on file — upload a resume for accurate scoring."
        )
    else:
        matched       = set(job_skills_lower) & user_skills_lower
        missing_raw   = list(set(job_skills_lower) - user_skills_lower)

        missing_core_count = 0
        for m in missing_raw:
            is_core = m in _CORE_TECH
            sev  = "High"            if is_core else "Medium"
            rec  = "Core requirement" if is_core else "Secondary skill"
            insights["missing_skills"].append(f"{m.title()} ({sev} severity) — {rec}")
            if is_core:
                missing_core_count += 1

        insights["missing_core"] = missing_core_count
        match_ratio = len(matched) / len(job_skills_lower)
        skill_match = 30.0 * match_ratio

        if missing_core_count > 0:
            skill_match = max(0.0, skill_match - missing_core_count * 8.0)
            insights["why_not"].append(f"Missing {missing_core_count} core skill(s).")

        if match_ratio >= 0.8 and missing_core_count == 0:
            insights["why_apply"].append("Exceptional skill match — no core gaps.")

    score += skill_match
    insights["skill_match"] = skill_match

    # ── 3. Experience match (20 pts) ──────────────────────────────────────────
    _LEVELS = ["intern", "entry", "mid", "senior", "staff"]
    job_exp  = job.experience_level or "mid"
    user_exp = (active_resume.experience_level if active_resume else None) or "mid"

    # Guard against unknown level strings
    job_lvl_idx  = _LEVELS.index(job_exp)  if job_exp  in _LEVELS else 2
    user_lvl_idx = _LEVELS.index(user_exp) if user_exp in _LEVELS else 2

    level_diff = abs(job_lvl_idx - user_lvl_idx)
    if level_diff == 0:
        exp_match = 20.0
        insights["why_apply"].append("Experience level aligns perfectly.")
    elif level_diff == 1:
        exp_match = 12.0
    else:
        exp_match = 5.0
        insights["why_not"].append(f"Experience gap (posting: {job_exp}, you: {user_exp}).")

    score += exp_match
    insights["experience_match"] = exp_match

    # ── 4. Location match (10 pts) ────────────────────────────────────────────
    target_locs = user.target_locations or []
    is_remote   = job.remote_type == "remote"
    loc_match   = 0.0

    if is_remote and user.open_to_remote:
        loc_match = 10.0
        insights["why_apply"].append("Remote role fits your preferences.")
    elif job.country and job.country in target_locs:
        loc_match = 10.0
    elif not target_locs:
        loc_match = 10.0
    else:
        loc_match = 2.0
        insights["why_not"].append("Location may require relocation.")

    score += loc_match
    insights["location_match"] = loc_match

    # ── 5. Recency (−15 to +10) ───────────────────────────────────────────────
    recency_match = 0.0
    days_old      = 0
    job_date      = job.posted_at or job.first_seen_at

    if job_date:
        now = datetime.datetime.now(datetime.timezone.utc)
        if job_date.tzinfo is None:
            job_date = job_date.replace(tzinfo=datetime.timezone.utc)
        diff     = now - job_date
        days_old = diff.days

        if days_old <= 1:
            recency_match = 10.0
            insights["why_apply"].append("Very fresh posting — apply today.")
        elif days_old <= 3:
            recency_match = 5.0
        elif days_old > 30:
            recency_match = -15.0
            insights["why_not"].append("Stale posting (>30 days old).")
        elif days_old > 14:
            recency_match = -8.0

    score += recency_match
    insights["recency_match"] = recency_match

    # ── 6. Company signal (up to 10 pts) ──────────────────────────────────────
    comp_signal = 0.0
    source_type = job.source_type or "unknown"

    if job.source_confidence and job.source_confidence >= 0.9:
        comp_signal += 5.0
        insights["why_apply"].append("High-quality verified source (direct ATS).")

    _premium = {
        "google", "anthropic", "meta", "openai", "stripe", "netflix",
        "deepmind", "apple", "microsoft", "amazon",
    }
    is_premium = bool(job.company_name) and job.company_name.lower() in _premium
    if is_premium:
        comp_signal += 5.0
        insights["why_apply"].append("Top-tier premium company.")

    score += comp_signal
    insights["company_signal"] = comp_signal

    # ── Competition / effort model ─────────────────────────────────────────────
    # source_type values stored by classify_source:
    #   "DIRECT_COMPANY" | "STAFFING_AGENCY" | "CONSULTING_VENDOR" | "UNKNOWN"
    # Realtime monitor stores the raw ats_type string (greenhouse, lever, etc.)
    effort     = 2.0
    comp_level = "Medium"
    if is_remote:
        comp_level = "Very High (global pool)"
    elif is_premium:
        comp_level = "High (premium tier)"
    elif source_type in ("STAFFING_AGENCY", "CONSULTING_VENDOR") and days_old > 3:
        comp_level = "High (public board)"

    if "workday" in (job.job_url or "").lower():
        effort = 3.5
        insights["why_not"].append("Workday portal — high-effort application.")
    elif source_type == "DIRECT_COMPANY":
        # Direct company ATS posting — typically lower friction than aggregator
        effort = 1.2

    insights["competitiveness"] = comp_level

    # ── 7. Confidence calculation ─────────────────────────────────────────────
    # Base: average of three sub-signals.
    # Then scale by the accumulated confidence_mult from data-quality checks.
    # Multiplier approach (vs hard cap): preserves proportionality — a job with
    # strong title+experience but no skills still scores, just with less certainty.
    skill_signal = (
        1.0 if (job_skills_lower and user_skills_lower)
        else 0.2 if not has_skills_in_db
        else 0.4
    )
    title_strength  = 1.0 if title_match >= 15 else 0.5
    fields_present  = sum(1 for x in [
        job.title, job.required_skills, job.experience_level, job.salary_min, job.remote_type
    ] if x)
    data_comp_ratio = fields_present / 5.0

    raw_confidence  = (skill_signal + title_strength + data_comp_ratio) / 3.0
    # Apply accumulated multiplier — never raises, only reduces
    confidence      = round(raw_confidence * min(confidence_mult, 1.0), 2)
    insights["confidence"] = confidence

    # ── 8. Data-quality score caps (P0 Fix) ──────────────────────────────────
    # Capping ensures thin-data jobs don't auto-apply (threshold 70), but strong
    # title matches should still be visible in REVIEW (threshold 50-60).
    if data_quality == "thin":
        # Allow up to 60 if title match is very strong
        cap = 60.0 if title_match >= 20 else 50.0
        if score > cap:
            score = cap
            insights["why_not"].append(
                f"Score capped at {cap} — insufficient job data for higher confidence."
            )
    elif data_quality == "partial":
        # Allow up to 70 if title match is strong
        cap = 75.0 if title_match >= 20 else 65.0
        if score > cap:
            score = cap

    capped_score = max(0.0, min(100.0, score))

    # ── 9. Interview probability & ROI ────────────────────────────────────────
    base_prob          = (capped_score / 100.0) * 0.45
    recency_multiplier = 1.2 if days_old <= 3 else (0.4 if days_old > 14 else 1.0)
    source_conf        = job.source_confidence or 0.5
    prob               = base_prob * recency_multiplier * (0.8 + source_conf * 0.5)

    if user_stats.get("success_rate", 0) > 0.1:
        prob *= 1.2

    prob                        = min(0.95, prob)
    insights["interview_probability"] = round(prob, 2)
    insights["roi_score"]             = round(prob / effort, 3)

    # ── 10. Urgency ────────────────────────────────────────────────────────────
    if capped_score > 75 and days_old <= 2:
        insights["apply_within_hours"] = 12
    elif capped_score > 60:
        insights["apply_within_hours"] = 24
    else:
        insights["apply_within_hours"] = 48

    avg_top = user_stats.get("top_scores_avg", 0)
    if avg_top > 0 and capped_score >= avg_top:
        insights["why_apply"].append(
            f"Top-tier: outperforms your historical baseline ({round(avg_top)})."
        )

    return round(capped_score, 1), insights


# ── DB-level decision (creates / retrieves AIDecision row) ────────────────────

async def evaluate_job_decision(
    db: AsyncSession,
    job: Job,
    user: User,
    strategy: str = "balanced",
) -> AIDecision:
    """
    Return the cached AIDecision for (job, user), creating it if absent.

    Decisions are intentionally cached — re-scoring only happens when the
    existing row is deleted or via POST /jobs/{id}/analyze.
    """
    stmt   = select(AIDecision).where(
        AIDecision.job_id == job.id, AIDecision.user_id == user.id
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        return existing

    # Active resume
    res_result = await db.execute(
        select(ResumeVersion).where(
            ResumeVersion.user_id == user.id, ResumeVersion.is_active == True
        )
    )
    active_resume = res_result.scalar_one_or_none()

    # Step 7: 30-day rolling window — only count recent signal so stale data
    # from 6+ months ago doesn't dilute current performance metrics.
    from app.models.ai_models import AIDecisionFeedback
    _30_days_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

    fb_result = await db.execute(
        select(AIDecisionFeedback.outcome, Job.role_category)
        .select_from(AIDecisionFeedback)
        .join(AIDecision)
        .join(Job)
        .where(
            AIDecision.user_id == user.id,
            AIDecisionFeedback.user_action.in_(["interview", "offer"]),
            AIDecisionFeedback.created_at >= _30_days_ago,   # Step 7: rolling window
        )
    )
    successful_roles: set[str] = set()
    total_interviews = 0
    for _outcome, role_cat in fb_result.all():
        total_interviews += 1
        if role_cat:
            successful_roles.add(role_cat.lower())

    # Count APPLY_NOW decisions in the same 30-day window as the denominator.
    apply_count_q = await db.execute(
        select(func.count(AIDecision.id)).where(
            AIDecision.user_id == user.id,
            AIDecision.decision == DecisionType.APPLY_NOW.value,
            AIDecision.created_at >= _30_days_ago,           # Step 7: rolling window
        )
    )
    total_apply_decisions = int(apply_count_q.scalar() or 0)

    # Step 7: guard — don't trust a rate based on fewer than 5 decisions;
    # fall back to 0.0 so the engine doesn't over-apply based on noise.
    if total_apply_decisions < 5:
        success_rate = 0.0
    else:
        success_rate = total_interviews / total_apply_decisions

    user_stats = {
        "successful_roles": successful_roles,
        "success_rate":     success_rate,
        "top_scores_avg":   75.0,
    }

    score, insights = calculate_fit_score(job, user, active_resume, user_stats, strategy)

    # Strategy thresholds
    if strategy == "aggressive":
        threshold_apply, threshold_review = 60, 40
    elif strategy == "selective":
        threshold_apply, threshold_review = 80, 65
    else:  # balanced
        threshold_apply, threshold_review = 70, 50

    # Use canonical DecisionType values — must match decision_agent.py and
    # learning_engine.py query predicates.  "APPLY" / "REVIEW" are retired.
    if score >= threshold_apply:
        decision_type = DecisionType.APPLY_NOW
    elif score >= threshold_review:
        decision_type = DecisionType.TAILOR_RESUME_FIRST
    else:
        decision_type = DecisionType.SKIP

    # Upgrade: near-threshold + no core gaps
    if (decision_type == DecisionType.TAILOR_RESUME_FIRST
            and score >= (threshold_apply - 5)
            and insights.get("missing_core", 0) == 0):
        decision_type = DecisionType.APPLY_NOW
        insights["why_apply"].append(
            "Upgraded to APPLY_NOW: near-threshold with zero core skill gaps."
        )

    # Upgrade: high ROI
    if decision_type == DecisionType.SKIP and insights["roi_score"] > 0.35:
        decision_type = DecisionType.TAILOR_RESUME_FIRST
        insights["why_apply"].append(
            "Upgraded to TAILOR_RESUME_FIRST: high ROI (low effort, decent probability)."
        )

    # Downgrade: low confidence
    if decision_type == DecisionType.APPLY_NOW and insights["confidence"] < 0.5:
        decision_type = DecisionType.TAILOR_RESUME_FIRST
        insights["why_not"].append("Downgraded to TAILOR_RESUME_FIRST: low data confidence.")

    # Downgrade: thin data — never auto-apply on incomplete records
    if decision_type == DecisionType.APPLY_NOW and insights.get("data_quality") == "thin":
        decision_type = DecisionType.TAILOR_RESUME_FIRST
        insights["why_not"].append(
            "Downgraded to TAILOR_RESUME_FIRST: insufficient data — manual check recommended."
        )

    strat_text = (
        f"Mode: {strategy.title()} | "
        f"Competitiveness: {insights['competitiveness']} | "
        f"ROI: {insights['roi_score']} | "
        f"Data: {insights.get('data_quality', 'unknown')}"
    )

    decision = AIDecision(
        user_id=user.id,
        job_id=job.id,
        decision=decision_type,
        fit_score=score,
        role_match_score=insights["title_match"],
        skill_match_score=insights["skill_match"],
        seniority_match_score=insights["experience_match"],
        location_match_score=insights["location_match"],
        missing_skills=insights["missing_skills"],
        why_apply=insights["why_apply"],
        why_not=insights["why_not"],
        confidence=insights["confidence"],
        apply_within_hours=insights["apply_within_hours"],
        interview_probability=insights["interview_probability"],
        application_strategy=strat_text,
        data_quality_score=insights["data_quality_score"],
    )
    db.add(decision)
    await db.commit()
    await db.refresh(decision)
    return decision
