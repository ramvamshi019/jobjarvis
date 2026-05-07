"""AI Decision Engine — produces structured APPLY_NOW / SKIP / etc. decisions."""
from dataclasses import dataclass, field
from typing import Optional
import structlog

from app.ai.resume_matcher import MatchResult
from app.ai.interview_probability import estimate_interview_probability
from app.models.ai_models import DecisionType
from app.services.freshness import is_fresh_enough_for_ai

logger = structlog.get_logger(__name__)

DECISIONS = {
    DecisionType.APPLY_NOW:           "High fit, low risk, fresh job — apply immediately.",
    DecisionType.TAILOR_RESUME_FIRST: "Good fit but resume needs adjustment before applying.",
    DecisionType.SAVE_FOR_LATER:      "Moderate fit — save and review when time permits.",
    DecisionType.SKIP:                "Low fit or not relevant to your goals.",
    DecisionType.HIGH_RISK:           "Eligibility risk or work auth restriction — disqualified.",
    DecisionType.REVIEW_NEEDED:       "AI confidence too low — needs human review.",
}


@dataclass
class DecisionOutput:
    decision: DecisionType
    fit_score: float
    priority: str   # HIGH|MEDIUM|LOW
    confidence: float
    role_category: str
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    recommended_resume: str = ""
    why_apply: list[str] = field(default_factory=list)
    why_not: list[str] = field(default_factory=list)
    application_strategy: str = ""
    apply_within_hours: int = 24
    recruiter_message: str = ""
    resume_suggestions: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    interview_probability: float = 0.0


def make_decision(
    match: MatchResult,
    job: dict,
    user_preferences: dict = None,
    memory_adjustments: dict = None,
) -> DecisionOutput:
    """
    Determine the optimal action for a job given match scores, risk signals, and memory.
    """
    if user_preferences is None:
        user_preferences = {}
    if memory_adjustments is None:
        memory_adjustments = {}

    fit = match.fit_score
    role_cat = job.get("role_category", "Unknown")
    spam_score = job.get("spam_score", 0.0) or 0.0
    work_auth_flags = job.get("work_auth_flags_json", {}) or {}
    eligibility_risk = match.risk_score
    freshness = job.get("freshness_label", "active_unknown")
    source_type = job.get("source_type", "UNKNOWN")

    # Memory adjustments (from learning loop)
    fit_adj = memory_adjustments.get("fit_adjustment", 0.0)
    fit += fit_adj

    # ── HARD DISQUALIFIERS ─────────────────────────────────────────
    risk_flags = []

    if eligibility_risk >= 0.8:
        risk_flags.append("high_eligibility_risk")
    if spam_score >= 0.6:
        risk_flags.append("spam_detected")

    disqualifying_flags = {k for k in (work_auth_flags.get("flags") or [])
                           if k in ("us_citizen_only", "security_clearance_required", "greencard_only")}
    if disqualifying_flags:
        risk_flags.extend(list(disqualifying_flags))
        return DecisionOutput(
            decision=DecisionType.HIGH_RISK,
            fit_score=fit,
            priority="LOW",
            confidence=0.95,
            role_category=role_cat,
            risk_flags=risk_flags,
            why_not=[f"Disqualifying restriction: {', '.join(disqualifying_flags)}"],
            needs_human_review=False,
            matched_skills=match.matched_skills,
            missing_skills=match.missing_skills,
        )

    if spam_score >= 0.7:
        return DecisionOutput(
            decision=DecisionType.SKIP,
            fit_score=fit,
            priority="LOW",
            confidence=0.90,
            role_category=role_cat,
            risk_flags=risk_flags,
            why_not=["High spam score — likely fake or low-quality posting"],
        )

    # ── INTERVIEW PROBABILITY ──────────────────────────────────────
    ip_result = estimate_interview_probability(
        fit_score=fit,
        application_timing_score={"new_last_hour": 1.0, "new_last_6_hours": 0.9,
                                   "new_today": 0.7, "new_last_3_days": 0.5}.get(freshness, 0.3),
        role_match_score=match.role_match_score,
        resume_strength=match.skill_match_score,
        past_interview_rate=memory_adjustments.get("past_interview_rate", 0.15),
    )
    interview_prob = ip_result.interview_probability

    # ── CONFIDENCE CALCULATION ─────────────────────────────────────
    confidence = min(0.95, 0.5 + (match.skill_match_score * 0.3) + (match.role_match_score * 0.2))
    if freshness in ("new_last_hour", "new_last_6_hours"):
        confidence = min(confidence + 0.1, 0.95)

    # ── DECISION LOGIC ─────────────────────────────────────────────
    why_apply = []
    why_not = []
    resume_suggestions = []

    if fit >= 75 and match.role_match_score >= 0.7 and eligibility_risk < 0.5 and spam_score < 0.3:
        decision = DecisionType.APPLY_NOW
        priority = "HIGH" if fit >= 85 else "MEDIUM"
        apply_within_hours = 6 if freshness in ("new_last_hour", "new_last_6_hours") else 24
        why_apply = [
            f"Strong fit score: {fit:.0f}/100",
            f"Matched {len(match.matched_skills)} skills",
            f"Role match: {match.role_match_score:.0%}",
        ]
    elif fit >= 55 and match.role_match_score >= 0.5 and eligibility_risk < 0.7:
        if match.skill_match_score < 0.5 and len(match.missing_skills) > 2:
            decision = DecisionType.TAILOR_RESUME_FIRST
            priority = "MEDIUM"
            apply_within_hours = 48
            resume_suggestions = [f"Highlight {s}" for s in match.missing_skills[:3]]
            why_apply = [f"Good role fit: {role_cat}", f"Fit score: {fit:.0f}"]
            why_not = [f"Missing skills: {', '.join(match.missing_skills[:3])}"]
        else:
            decision = DecisionType.APPLY_NOW
            priority = "MEDIUM"
            apply_within_hours = 24
            why_apply = [f"Decent fit: {fit:.0f}/100", f"Role: {role_cat}"]
    elif fit >= 40:
        decision = DecisionType.SAVE_FOR_LATER
        priority = "LOW"
        apply_within_hours = 72
        why_not = [f"Fit score ({fit:.0f}) below threshold", f"Role match: {match.role_match_score:.0%}"]
    else:
        decision = DecisionType.SKIP
        priority = "LOW"
        apply_within_hours = 0
        why_not = [
            f"Low fit score: {fit:.0f}",
            f"Role mismatch: {role_cat}",
            f"Missing {len(match.missing_skills)} required skills",
        ]

    # Low confidence → human review
    needs_human_review = confidence < 0.65
    if needs_human_review:
        decision = DecisionType.REVIEW_NEEDED

    # ── Application strategy ───────────────────────────────────────
    strategy_parts = []
    if decision in (DecisionType.APPLY_NOW, DecisionType.TAILOR_RESUME_FIRST):
        if source_type == "DIRECT_COMPANY":
            strategy_parts.append("Apply directly — this is a direct company posting.")
        if freshness in ("new_last_hour", "new_last_6_hours"):
            strategy_parts.append("Job is very fresh — apply ASAP to beat competition.")
        if interview_prob >= 0.4:
            strategy_parts.append(f"Strong interview probability: {interview_prob:.0%}.")
        if match.missing_skills:
            strategy_parts.append(f"Address skill gaps in cover letter: {', '.join(match.missing_skills[:2])}.")

    return DecisionOutput(
        decision=decision,
        fit_score=round(fit, 1),
        priority=priority,
        confidence=round(confidence, 3),
        role_category=role_cat,
        matched_skills=match.matched_skills,
        missing_skills=match.missing_skills,
        risk_flags=risk_flags,
        recommended_resume=match.recommended_resume_version,
        why_apply=why_apply,
        why_not=why_not,
        application_strategy=" ".join(strategy_parts),
        apply_within_hours=apply_within_hours,
        resume_suggestions=resume_suggestions,
        needs_human_review=needs_human_review,
        interview_probability=interview_prob,
    )
