from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class JobOut(BaseModel):
    id: int
    company_id: int
    title: str
    normalized_title: Optional[str]
    company_name: str
    location: Optional[str]
    normalized_location: Optional[str]
    country: Optional[str]
    remote_type: Optional[str]
    job_url: Optional[str]
    employment_type: Optional[str]
    experience_level: Optional[str]
    role_category: Optional[str]
    salary_min: Optional[int]
    salary_max: Optional[int]
    salary_currency: Optional[str]
    required_skills: Optional[list[str]]
    preferred_skills: Optional[list[str]]
    spam_score: float
    eligibility_risk_score: float
    source_type: Optional[str]
    freshness_label: Optional[str]
    first_seen_at: datetime
    posted_at: Optional[datetime]
    active: bool
    decision: Optional[str] = None
    fit_score: Optional[float] = None

    class Config:
        from_attributes = True


class JobFilter(BaseModel):
    role_category: Optional[str] = None
    country: Optional[str] = None
    remote_type: Optional[str] = None
    timezone: Optional[str] = None
    experience_level: Optional[str] = None
    min_salary: Optional[int] = None
    freshness: Optional[str] = None  # new_today|new_last_6_hours|etc
    source_type: Optional[str] = None
    page: int = 1
    page_size: int = 25
