"""Resume-to-job matching engine."""
from dataclasses import dataclass, field
from typing import Optional
from app.ai.skill_extractor import match_skills_to_resume


@dataclass
class MatchResult:
    fit_score: float
    role_match_score: float
    skill_match_score: float
    seniority_match_score: float
    domain_match_score: float
    location_match_score: float
    compensation_match_score: float
    risk_score: float

    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    recommended_resume_version: str = ""
    explanation: str = ""


SENIORITY_COMPATIBILITY = {
    "intern":  {"intern": 1.0, "entry": 0.5, "mid": 0.2, "senior": 0.0},
    "entry":   {"intern": 0.8, "entry": 1.0, "mid": 0.5, "senior": 0.1},
    "mid":     {"intern": 0.2, "entry": 0.8, "mid": 1.0, "senior": 0.5},
    "senior":  {"intern": 0.0, "entry": 0.2, "mid": 0.7, "senior": 1.0},
}

ROLE_RESUME_MAP = {
    "AI Engineer": "ai_engineer",
    "ML Engineer": "ml_engineer",
    "Data Engineer": "data_engineer",
    "Data Platform Engineer": "data_engineer",
    "MLOps Engineer": "mlops",
    "Analytics Engineer": "analytics_engineer",
    "Backend Engineer": "backend",
    "QA/SDET": "sdet",
    "Software Engineer": "backend",
}


def compute_match(
    job: dict,
    resume: dict,
    user_preferences: dict = None,
) -> MatchResult:
    """
    Compute multi-dimensional match between a job and a resume profile.
    job: normalized job dict
    resume: parsed resume dict with keys: skills, target_roles, experience_level, etc.
    """
    if user_preferences is None:
        user_preferences = {}

    # ── Role match ────────────────────────────────────────────────────
    job_role = job.get("role_category", "")
    user_roles = resume.get("target_roles", []) or []
    if job_role in user_roles:
        role_match = 1.0
    elif any(r in job_role for r in user_roles) or any(job_role in r for r in user_roles):
        role_match = 0.7
    else:
        role_match = 0.3 if job_role not in ("Not Relevant", "Other") else 0.1

    # ── Skill match ───────────────────────────────────────────────────
    required_skills = job.get("required_skills", []) or []
    preferred_skills = job.get("preferred_skills", []) or []
    all_job_skills = list(dict.fromkeys(required_skills + preferred_skills))

    resume_skills = resume.get("skills", []) or []
    matched, missing = match_skills_to_resume(all_job_skills, resume_skills)
    matched_req, missing_req = match_skills_to_resume(required_skills, resume_skills)

    skill_match = len(matched) / max(len(all_job_skills), 1)
    req_skill_match = len(matched_req) / max(len(required_skills), 1)
    # Weight required skills more heavily
    skill_match_score = 0.7 * req_skill_match + 0.3 * skill_match

    # ── Seniority match ───────────────────────────────────────────────
    user_level = resume.get("experience_level", "mid")
    job_level = job.get("experience_level", "mid")
    seniority_compat = SENIORITY_COMPATIBILITY.get(user_level, {})
    seniority_match = seniority_compat.get(job_level, 0.5)

    # ── Domain match ──────────────────────────────────────────────────
    user_domain_tools = set((resume.get("tools", []) or []) + (resume.get("cloud_platforms", []) or []))
    job_tools = set(job.get("matched_tools", []) or [])
    domain_match = len(user_domain_tools & job_tools) / max(len(job_tools), 1) if job_tools else 0.5

    # ── Location match ────────────────────────────────────────────────
    job_remote = job.get("remote_type", "onsite")
    user_open_remote = user_preferences.get("open_to_remote", True)
    target_locations = user_preferences.get("target_locations", []) or []
    job_location = job.get("normalized_location", "")

    if job_remote == "remote":
        location_match = 1.0
    elif target_locations and any(loc.lower() in job_location.lower() for loc in target_locations):
        location_match = 1.0
    elif job_remote == "hybrid":
        location_match = 0.7
    else:
        location_match = 0.4

    # ── Compensation match ────────────────────────────────────────────
    user_min_salary = user_preferences.get("min_salary", 0) or 0
    job_salary_max = job.get("salary_max") or 0
    job_salary_min = job.get("salary_min") or 0

    if job_salary_min and job_salary_max:
        if job_salary_max >= user_min_salary:
            compensation_match = 1.0
        elif job_salary_min >= user_min_salary * 0.8:
            compensation_match = 0.7
        else:
            compensation_match = 0.3
    else:
        compensation_match = 0.5  # unknown salary

    # ── Risk score ────────────────────────────────────────────────────
    risk_score = job.get("eligibility_risk_score", 0.0) or 0.0

    # ── Composite fit score ───────────────────────────────────────────
    fit_score = (
        role_match * 0.25
        + skill_match_score * 0.30
        + seniority_match * 0.15
        + domain_match * 0.10
        + location_match * 0.10
        + compensation_match * 0.10
    ) * 100.0
    fit_score = max(0.0, min(100.0, fit_score))
    fit_score -= risk_score * 20  # penalize risk
    fit_score = max(0.0, fit_score)

    # ── Recommend resume version ──────────────────────────────────────
    recommended = ROLE_RESUME_MAP.get(job_role, "default")

    reasons = []
    if role_match >= 0.8:
        reasons.append(f"Strong role match: {job_role}")
    if skill_match_score >= 0.7:
        reasons.append(f"Good skill coverage ({len(matched)}/{len(all_job_skills)} skills matched)")
    if missing_req:
        reasons.append(f"Missing required skills: {', '.join(missing_req[:3])}")

    return MatchResult(
        fit_score=round(fit_score, 1),
        role_match_score=round(role_match, 3),
        skill_match_score=round(skill_match_score, 3),
        seniority_match_score=round(seniority_match, 3),
        domain_match_score=round(domain_match, 3),
        location_match_score=round(location_match, 3),
        compensation_match_score=round(compensation_match, 3),
        risk_score=round(risk_score, 3),
        matched_skills=matched,
        missing_skills=missing,
        recommended_resume_version=recommended,
        explanation="; ".join(reasons),
    )
