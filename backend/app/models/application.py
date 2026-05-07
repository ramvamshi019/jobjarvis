"""Application tracking, answer bank, and outreach models."""
import enum
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, Integer, Text, JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.database import Base


class ApplicationStatus(str, enum.Enum):
    SAVED = "saved"
    FORM_PENDING = "form_pending"
    MANUAL_REQUIRED = "manual_required"
    APPLIED = "applied"
    RECRUITER_CONTACTED = "recruiter_contacted"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    CLOSED = "closed"


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    resume_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resume_versions.id"))
    status: Mapped[ApplicationStatus] = mapped_column(
        String(50), default=ApplicationStatus.SAVED, index=True
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    follow_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    recruiter_name: Mapped[Optional[str]] = mapped_column(String(255))
    recruiter_email: Mapped[Optional[str]] = mapped_column(String(255))
    recruiter_linkedin: Mapped[Optional[str]] = mapped_column(String(500))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    outcome: Mapped[Optional[str]] = mapped_column(String(100))
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(500))
    interview_rounds: Mapped[int] = mapped_column(Integer, default=0)
    cover_letter: Mapped[Optional[str]] = mapped_column(Text)
    platform_used: Mapped[Optional[str]] = mapped_column(String(100))  # greenhouse|lever|company|linkedin

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[object] = relationship("User", back_populates="applications", lazy="select")
    job: Mapped[object] = relationship("Job", back_populates="applications", lazy="select")
    resume_version: Mapped[Optional[object]] = relationship(
        "ResumeVersion", back_populates="applications", lazy="select"
    )

    __table_args__ = (
        Index("ix_app_user_status", "user_id", "status"),
        Index("ix_app_user_job", "user_id", "job_id"),
    )


class ApplicationAnswer(Base):
    __tablename__ = "application_answers"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    question_type: Mapped[str] = mapped_column(String(100), index=True)
    question_text: Mapped[Optional[str]] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_role: Mapped[Optional[str]] = mapped_column(String(100))
    tone: Mapped[Optional[str]] = mapped_column(String(50))  # professional|casual|confident
    word_count: Mapped[Optional[int]] = mapped_column(Integer)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OutreachMessage(Base):
    __tablename__ = "outreach_messages"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    message_type: Mapped[str] = mapped_column(String(50))  # recruiter_email|linkedin_dm|cover_letter|follow_up_3d|follow_up_7d
    subject: Mapped[Optional[str]] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="draft")  # draft|sent|replied
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
