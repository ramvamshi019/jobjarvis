from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class DecisionOut(BaseModel):
    id: int
    job_id: int
    decision: str
    fit_score: Optional[float]
    priority: Optional[str]
    confidence: Optional[float]
    role_category: Optional[str]
    data_quality_score: Optional[float]
    matched_skills: Optional[list[str]]
    missing_skills: Optional[list[str]]
    risk_flags: Optional[list[str]]
    recommended_resume: Optional[str]
    why_apply: Optional[list[str]]
    why_not: Optional[list[str]]
    application_strategy: Optional[str]
    apply_within_hours: Optional[int]
    recruiter_message: Optional[str]
    resume_suggestions: Optional[list[str]]
    needs_human_review: bool
    interview_probability: Optional[float]
    created_at: datetime

    class Config:
        from_attributes = True


class FeedbackRequest(BaseModel):
    user_action: str   # applied|skipped|saved|rejected_later|interview|offer
    outcome: Optional[str] = None  # positive|negative|neutral
    feedback_notes: Optional[str] = None
