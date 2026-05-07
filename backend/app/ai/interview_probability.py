"""Estimate interview probability for a job given user profile and signals."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class InterviewProbabilityResult:
    interview_probability: float
    factors: dict


def estimate_interview_probability(
    fit_score: float,
    company_hiring_velocity: float = 0.5,
    role_match_score: float = 0.5,
    resume_strength: float = 0.6,
    competition_estimate: float = 0.5,  # 0=low, 1=high
    application_timing_score: float = 0.7,  # freshness proxy
    past_interview_rate: float = 0.2,  # historical rate from memory
) -> InterviewProbabilityResult:
    """
    Bayesian-inspired estimate using weighted signals.
    Returns 0.0-1.0 probability.
    """
    base_rate = 0.15  # industry average ~15%

    # Adjustments
    fit_adj = (fit_score / 100.0 - 0.5) * 0.3          # ±0.15
    velocity_adj = (company_hiring_velocity - 0.5) * 0.1  # ±0.05
    role_adj = (role_match_score - 0.5) * 0.2            # ±0.10
    resume_adj = (resume_strength - 0.5) * 0.15          # ±0.075
    competition_adj = (0.5 - competition_estimate) * 0.1  # negative if high comp
    timing_adj = (application_timing_score - 0.5) * 0.1  # fresh = better
    history_adj = (past_interview_rate - 0.2) * 0.5      # personal history weight

    probability = (
        base_rate
        + fit_adj + velocity_adj + role_adj
        + resume_adj + competition_adj + timing_adj
        + history_adj
    )
    probability = max(0.02, min(0.95, probability))

    return InterviewProbabilityResult(
        interview_probability=round(probability, 3),
        factors={
            "fit_adj": round(fit_adj, 3),
            "velocity_adj": round(velocity_adj, 3),
            "role_adj": round(role_adj, 3),
            "resume_adj": round(resume_adj, 3),
            "competition_adj": round(competition_adj, 3),
            "timing_adj": round(timing_adj, 3),
            "history_adj": round(history_adj, 3),
        }
    )
