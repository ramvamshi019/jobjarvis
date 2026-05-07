"""User model with role-based access."""
import enum
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, Enum as SAEnum, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.database import Base


class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Job preferences
    target_roles: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    target_locations: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    preferred_employment_types: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    min_salary: Mapped[Optional[int]] = mapped_column()
    max_commute_miles: Mapped[Optional[int]] = mapped_column()
    open_to_remote: Mapped[bool] = mapped_column(Boolean, default=True)
    work_authorization: Mapped[Optional[str]] = mapped_column(String(100))
    current_location: Mapped[Optional[str]] = mapped_column(String(255))

    # Notification preferences
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_min_fit_score: Mapped[int] = mapped_column(default=75)

    # Profile
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(500))
    github_url: Mapped[Optional[str]] = mapped_column(String(500))
    portfolio_url: Mapped[Optional[str]] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    resumes: Mapped[list] = relationship("ResumeVersion", back_populates="user", lazy="select")
    applications: Mapped[list] = relationship("Application", back_populates="user", lazy="select")
    memories: Mapped[list] = relationship("AIMemory", back_populates="user", lazy="select")
    decisions: Mapped[list] = relationship("AIDecision", back_populates="user", lazy="select")
