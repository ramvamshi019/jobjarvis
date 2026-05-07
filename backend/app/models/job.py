"""Job and job change-tracking models."""
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    String, Boolean, DateTime, Integer, Float, Text, JSON,
    BigInteger, ForeignKey, Index, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.database import Base, BigIntPK


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(500), index=True)

    # Core fields
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_title: Mapped[Optional[str]] = mapped_column(String(500), index=True)
    company_name: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(500))
    normalized_location: Mapped[Optional[str]] = mapped_column(String(500), index=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    region: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    remote_type: Mapped[Optional[str]] = mapped_column(String(50))  # remote|hybrid|onsite|unknown

    # URLs
    job_url: Mapped[Optional[str]] = mapped_column(Text, unique=True, index=True, name="url")
    apply_url: Mapped[Optional[str]] = mapped_column(Text)

    # Description
    description: Mapped[Optional[str]] = mapped_column(Text)
    description_html: Mapped[Optional[str]] = mapped_column(Text)

    # Classification
    employment_type: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    experience_level: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    role_category: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    role_confidence: Mapped[Optional[float]] = mapped_column(Float)

    # Salary
    salary_min: Mapped[Optional[int]] = mapped_column(Integer)
    salary_max: Mapped[Optional[int]] = mapped_column(Integer)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(10))
    salary_period: Mapped[Optional[str]] = mapped_column(String(20))  # annual|monthly|hourly

    # Skills
    required_skills: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    preferred_skills: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    matched_tools: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    # Quality signals
    spam_score: Mapped[float] = mapped_column(Float, default=0.0)
    spam_flags_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    work_auth_flags_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    eligibility_risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    data_quality_score: Mapped[Optional[float]] = mapped_column(Float, default=0.0)  # Step 9
    source_type: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    source_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    # Dedup
    fingerprint: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    raw_hash: Mapped[Optional[str]] = mapped_column(String(64))

    # Timing
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    freshness_label: Mapped[Optional[str]] = mapped_column(String(50))
    freshness_score: Mapped[Optional[float]] = mapped_column(Float, default=0.0, index=True)

    # Status
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source: Mapped[Optional[str]] = mapped_column(String(100))  # ats name

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    company: Mapped[object] = relationship("Company", back_populates="jobs", lazy="select")
    status_history: Mapped[list] = relationship("JobStatusHistory", back_populates="job", lazy="select")
    decisions: Mapped[list] = relationship("AIDecision", back_populates="job", lazy="select")
    applications: Mapped[list] = relationship("Application", back_populates="job", lazy="select")
    embedding: Mapped[Optional[object]] = relationship(
        "JobEmbedding", back_populates="job", uselist=False, lazy="select"
    )

    __table_args__ = (
        UniqueConstraint("company_id", "external_id", name="uq_job_company_external"),
        Index("ix_jobs_active_fresh", "active", "first_seen_at"),
        Index("ix_jobs_freshness_active", "freshness_score", "active"),
        Index("ix_jobs_role_active", "role_category", "active"),
        Index("ix_jobs_country_active", "country", "active"),
        Index("ix_jobs_fingerprint_active", "fingerprint", "active"),
        Index("ix_jobs_last_seen_active", "last_seen_at", "active"),
    )


class JobStatusHistory(Base):
    __tablename__ = "job_status_history"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    old_status: Mapped[Optional[str]] = mapped_column(String(50))
    new_status: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(500))
    field_changed: Mapped[Optional[str]] = mapped_column(String(100))
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    job: Mapped[object] = relationship("Job", back_populates="status_history", lazy="select")
