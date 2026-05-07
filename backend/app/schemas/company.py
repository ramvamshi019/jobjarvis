from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class CompanyIn(BaseModel):
    name: str
    domain: Optional[str] = None
    career_url: Optional[str] = None
    ats_type: Optional[str] = None
    ats_identifier: Optional[str] = None
    country: Optional[str] = None
    priority_score: int = 50
    scan_frequency_minutes: int = 360


class CompanyOut(BaseModel):
    id: int
    name: str
    domain: Optional[str]
    ats_type: Optional[str]
    ats_identifier: Optional[str]
    country: Optional[str]
    priority_score: int
    scan_frequency_minutes: int
    active: bool
    last_checked_at: Optional[datetime]
    last_success_at: Optional[datetime]
    failure_count: int
    consecutive_failures: int
    scan_tier: str

    class Config:
        from_attributes = True
