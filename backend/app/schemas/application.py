from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class ApplicationIn(BaseModel):
    job_id: int
    resume_version_id: Optional[int] = None
    status: str = "saved"
    notes: Optional[str] = None
    cover_letter: Optional[str] = None


class ApplicationOut(BaseModel):
    id: int
    job_id: int
    user_id: int
    status: str
    resume_version_id: Optional[int]
    applied_at: Optional[datetime]
    follow_up_at: Optional[datetime]
    recruiter_name: Optional[str]
    recruiter_email: Optional[str]
    notes: Optional[str]
    outcome: Optional[str]
    interview_rounds: int
    created_at: datetime

    class Config:
        from_attributes = True


class ApplicationUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    recruiter_name: Optional[str] = None
    recruiter_email: Optional[str] = None
    outcome: Optional[str] = None
    interview_rounds: Optional[int] = None
    follow_up_at: Optional[datetime] = None
