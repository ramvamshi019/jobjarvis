"""Gold layer ranking engine."""
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)

# Weights (sum to 1.0)
WEIGHTS = {
    "fit_score": 0.30,
    "freshness": 0.20,
    "company_score": 0.12,
    "interview_probability": 0.15,
    "application_difficulty": 0.05,  # lower = better
    "work_auth_risk": 0.08,          # lower = better
    "source_quality": 0.05,
    "spam_score": 0.05,              # lower = better
}

FRESHNESS_SCORES = {
    "new_last_hour": 1.0,
    "new_last_6_hours": 0.9,
    "new_today": 0.7,
    "new_last_3_days": 0.5,
    "stale": 0.2,
    "active_unknown": 0.3,
    "reposted": 0.4,
}

SOURCE_QUALITY = {
    "DIRECT_COMPANY": 1.0,
    "STAFFING_AGENCY": 0.5,
    "CONSULTING_VENDOR": 0.3,
    "UNKNOWN": 0.6,
}


def compute_rank_score(
    fit_score: float = 50.0,
    freshness_label: str = "active_unknown",
    company_score: float = 50.0,
    interview_probability: float = 0.3,
    spam_score: float = 0.0,
    eligibility_risk_score: float = 0.0,
    source_type: str = "UNKNOWN",
    application_difficulty: float = 0.3,  # 0=easy, 1=hard
) -> float:
    """Composite rank score 0-100."""

    freshness_val = FRESHNESS_SCORES.get(freshness_label, 0.3)
    source_val = SOURCE_QUALITY.get(source_type, 0.6)

    score = (
        (fit_score / 100.0) * WEIGHTS["fit_score"]
        + freshness_val * WEIGHTS["freshness"]
        + (company_score / 100.0) * WEIGHTS["company_score"]
        + interview_probability * WEIGHTS["interview_probability"]
        + (1.0 - application_difficulty) * WEIGHTS["application_difficulty"]
        + (1.0 - eligibility_risk_score) * WEIGHTS["work_auth_risk"]
        + source_val * WEIGHTS["source_quality"]
        + (1.0 - spam_score) * WEIGHTS["spam_score"]
    )
    return round(score * 100, 2)
