"""Resume version control model."""
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, Integer, Text, JSON, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.database import Base


class ResumeVersion(Base):
    __tablename__ = "resume_versions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_role: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    version_tag: Mapped[Optional[str]] = mapped_column(String(100))  # v1, v2, etc.

    # Content
    content: Mapped[Optional[str]] = mapped_column(Text)         # raw text
    content_html: Mapped[Optional[str]] = mapped_column(Text)    # HTML version
    file_path: Mapped[Optional[str]] = mapped_column(String(500))  # stored file path
    file_type: Mapped[Optional[str]] = mapped_column(String(20))   # pdf|docx|txt

    # Parsed structured data
    skills_json: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    projects_json: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    experience_json: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    education_json: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    certifications_json: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    tools_json: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    cloud_platforms_json: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    # Metadata
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    experience_level: Mapped[Optional[str]] = mapped_column(String(50))  # intern|entry|mid|senior
    overall_strength_score: Mapped[Optional[float]] = mapped_column()
    ats_score: Mapped[Optional[float]] = mapped_column()
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped[object] = relationship("User", back_populates="resumes", lazy="select")
    applications: Mapped[list] = relationship("Application", back_populates="resume_version", lazy="select")
    embedding: Mapped[Optional[object]] = relationship(
        "ResumeEmbedding", back_populates="resume", uselist=False, lazy="select"
    )

    __table_args__ = (
        Index("ix_resume_user_active", "user_id", "is_active"),
        Index("ix_resume_user_role", "user_id", "target_role"),
    )
