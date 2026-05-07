"""All AI-related models: memory, decisions, prompts, embeddings, intelligence."""
import enum
from datetime import datetime
from typing import Optional


class DecisionType(str, enum.Enum):
    """Canonical decision labels used by ALL parts of the system.

    Both the CareerAgent pipeline (decision_agent.py) and the API-level engine
    (decision_engine.py) must write only these values.  Any query filtering on
    AIDecision.decision must use these constants, not bare strings.
    """
    APPLY_NOW           = "APPLY_NOW"
    TAILOR_RESUME_FIRST = "TAILOR_RESUME_FIRST"
    SAVE_FOR_LATER      = "SAVE_FOR_LATER"
    SKIP                = "SKIP"
    HIGH_RISK           = "HIGH_RISK"
    REVIEW_NEEDED       = "REVIEW_NEEDED"
from sqlalchemy import (
    String, Boolean, DateTime, Integer, Float, Text, JSON,
    BigInteger, ForeignKey, Index, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
try:
    from pgvector.sqlalchemy import Vector
    _VECTOR_AVAILABLE = True
except ImportError:
    _VECTOR_AVAILABLE = False
from app.database import Base, BigIntPK
from app.config import settings


class AIMemory(Base):
    __tablename__ = "ai_memory"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(100), index=True)
    # Types: skill_signal|company_signal|role_signal|seniority_signal|outcome_pattern|correction
    insight: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    applied_count: Mapped[int] = mapped_column(Integer, default=0)
    last_applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    user: Mapped[object] = relationship("User", back_populates="memories", lazy="select")


class AIDecision(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(50), index=True)
    # APPLY_NOW|TAILOR_RESUME_FIRST|SAVE_FOR_LATER|SKIP|HIGH_RISK|REVIEW_NEEDED

    fit_score: Mapped[Optional[float]] = mapped_column(Float)
    role_match_score: Mapped[Optional[float]] = mapped_column(Float)
    skill_match_score: Mapped[Optional[float]] = mapped_column(Float)
    seniority_match_score: Mapped[Optional[float]] = mapped_column(Float)
    domain_match_score: Mapped[Optional[float]] = mapped_column(Float)
    location_match_score: Mapped[Optional[float]] = mapped_column(Float)
    compensation_match_score: Mapped[Optional[float]] = mapped_column(Float)
    risk_score: Mapped[Optional[float]] = mapped_column(Float)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    interview_probability: Mapped[Optional[float]] = mapped_column(Float)
    priority: Mapped[Optional[str]] = mapped_column(String(20))  # HIGH|MEDIUM|LOW
    role_category: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    data_quality_score: Mapped[Optional[float]] = mapped_column(Float)

    matched_skills: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    missing_skills: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    risk_flags: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    recommended_resume: Mapped[Optional[str]] = mapped_column(String(255))
    why_apply: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    why_not: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    application_strategy: Mapped[Optional[str]] = mapped_column(Text)
    apply_within_hours: Mapped[Optional[int]] = mapped_column(Integer)
    recruiter_message: Mapped[Optional[str]] = mapped_column(Text)
    resume_suggestions: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(50))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[object] = relationship("User", back_populates="decisions", lazy="select")
    job: Mapped[object] = relationship("Job", back_populates="decisions", lazy="select")
    feedback: Mapped[Optional[object]] = relationship(
        "AIDecisionFeedback", back_populates="decision", uselist=False, lazy="select"
    )

    __table_args__ = (
        Index("ix_decision_user_job", "user_id", "job_id"),
        Index("ix_decision_user_type", "user_id", "decision"),
    )


class AIDecisionFeedback(Base):
    __tablename__ = "ai_decision_feedback"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    ai_decision_id: Mapped[int] = mapped_column(
        ForeignKey("ai_decisions.id"), nullable=False, index=True
    )
    user_action: Mapped[str] = mapped_column(String(50))  # applied|skipped|saved|rejected_later|interview|offer
    outcome: Mapped[Optional[str]] = mapped_column(String(50))  # positive|negative|neutral
    feedback_notes: Mapped[Optional[str]] = mapped_column(Text)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decision: Mapped[object] = relationship("AIDecision", back_populates="feedback", lazy="select")


class HumanReviewQueue(Base):
    __tablename__ = "human_review_queue"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[float] = mapped_column(Float)
    ai_decision_json: Mapped[Optional[dict]] = mapped_column(JSON)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    user_decision: Mapped[Optional[str]] = mapped_column(String(50))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIPrompt(Base):
    __tablename__ = "ai_prompts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    prompt_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("prompt_name", "version", name="uq_prompt_name_version"),
    )


class AIUsageLog(Base):
    __tablename__ = "ai_usage_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, index=True)
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(100))
    task_type: Mapped[Optional[str]] = mapped_column(String(100))  # role_classify|skill_extract|decision|etc
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[Optional[str]] = mapped_column(String(500))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompanyIntelligence(Base):
    __tablename__ = "company_intelligence"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"), nullable=False, unique=True, index=True
    )
    company_score: Mapped[float] = mapped_column(Float, default=50.0)
    hiring_velocity: Mapped[float] = mapped_column(Float, default=0.0)
    jobs_last_7_days: Mapped[int] = mapped_column(Integer, default=0)
    jobs_last_30_days: Mapped[int] = mapped_column(Integer, default=0)
    ai_data_hiring_score: Mapped[float] = mapped_column(Float, default=0.0)
    remote_score: Mapped[float] = mapped_column(Float, default=0.0)
    sponsorship_score: Mapped[float] = mapped_column(Float, default=0.0)
    direct_company_score: Mapped[float] = mapped_column(Float, default=1.0)
    notes_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    company: Mapped[object] = relationship("Company", back_populates="intelligence", lazy="select")


class JobEmbedding(Base):
    __tablename__ = "job_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, unique=True, index=True)
    model: Mapped[str] = mapped_column(String(100), default="text-embedding-3-small")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[object] = relationship("Job", back_populates="embedding", lazy="select")

    # P0-5 fix: add the embedding column to the ORM so it can be read/written
    # via SQLAlchemy instead of only through raw SQL in migrations.
    # Uses pgvector.sqlalchemy.Vector when available; falls back to JSON for
    # SQLite / environments without pgvector installed (dev / CI).
    if _VECTOR_AVAILABLE:
        embedding = mapped_column(Vector(settings.VECTOR_DIMENSIONS), nullable=True)
    else:
        embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)


class ResumeEmbedding(Base):
    __tablename__ = "resume_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resume_versions.id"), nullable=False, unique=True, index=True
    )
    model: Mapped[str] = mapped_column(String(100), default="text-embedding-3-small")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    resume: Mapped[object] = relationship("ResumeVersion", back_populates="embedding", lazy="select")

    # P0-5 fix: same pattern as JobEmbedding above
    if _VECTOR_AVAILABLE:
        embedding = mapped_column(Vector(settings.VECTOR_DIMENSIONS), nullable=True)
    else:
        embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)


class DataQualityReport(Base):
    __tablename__ = "data_quality_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    total_jobs: Mapped[int] = mapped_column(Integer, default=0)
    missing_title: Mapped[int] = mapped_column(Integer, default=0)
    missing_company: Mapped[int] = mapped_column(Integer, default=0)
    missing_url: Mapped[int] = mapped_column(Integer, default=0)
    invalid_date: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    stale_jobs: Mapped[int] = mapped_column(Integer, default=0)
    broken_urls: Mapped[int] = mapped_column(Integer, default=0)
    empty_description: Mapped[int] = mapped_column(Integer, default=0)
    report_json: Mapped[Optional[dict]] = mapped_column(JSON)


class FetchAuditLog(Base):
    __tablename__ = "fetch_audit_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    url: Mapped[Optional[str]] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(10), default="GET")
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("companies.id"), index=True)
    scan_type: Mapped[str] = mapped_column(String(50), default="scheduled")
    status: Mapped[str] = mapped_column(String(50), default="running", index=True)
    jobs_fetched: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    jobs_closed: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    raw_response_path: Mapped[Optional[str]] = mapped_column(String(500))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class BronzeRawJob(Base):
    """Bronze layer: raw unprocessed job data."""
    __tablename__ = "bronze_raw_jobs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    scan_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scan_runs.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(50))
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON)
    raw_html: Mapped[Optional[str]] = mapped_column(Text)
    external_id: Mapped[Optional[str]] = mapped_column(String(500))
    raw_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    silver_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"))

    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
