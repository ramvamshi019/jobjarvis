"""Company registry model supporting 40K+ companies."""
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, Integer, Float, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.database import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, unique=True, index=True)
    domain: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True)
    career_url: Mapped[Optional[str]] = mapped_column(Text, name="careers_url")
    ats_type: Mapped[Optional[str]] = mapped_column(String(50), index=True, name="ats")  # greenhouse|lever|ashby|smartrecruiters|workday|icims|other
    ats_identifier: Mapped[Optional[str]] = mapped_column(String(500))       # slug / board token / company_id
    country: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    industry: Mapped[Optional[str]] = mapped_column(String(200))
    size_range: Mapped[Optional[str]] = mapped_column(String(50))            # startup|smb|mid|large|enterprise
 
    # Scheduling + smart prioritization
    priority_score: Mapped[int] = mapped_column(Integer, default=50, index=True)
    scan_priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    scan_frequency_minutes: Mapped[int] = mapped_column(Integer, default=360)
    last_job_found_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    jobs_found_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), name="last_scanned_at")
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    next_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Flags
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_blocklisted: Mapped[bool] = mapped_column(Boolean, default=False)
    robots_txt_compliant: Mapped[bool] = mapped_column(Boolean, default=True)

    # Notes
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    jobs: Mapped[list] = relationship("Job", back_populates="company", lazy="select")
    intelligence: Mapped[Optional[object]] = relationship(
        "CompanyIntelligence", back_populates="company", uselist=False, lazy="select"
    )

    __table_args__ = (
        Index("ix_companies_active_next_scan", "active", "next_scan_at"),
        Index("ix_companies_priority_active", "priority_score", "active"),
    )

    @property
    def scan_tier(self) -> str:
        if self.priority_score >= 90:
            return "tier1"
        elif self.priority_score >= 60:
            return "tier2"
        elif self.priority_score >= 20:
            return "tier3"
        return "tier4"
