from sqlalchemy import Column, String, Integer, BigInteger, Float, DateTime, Date, Boolean, ForeignKey, Text, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone, date
from typing import List

from src.models.base import Base


class UserInDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="student")
    avatar_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    refresh_token = Column(String, nullable=True)
    push_token = Column(String, nullable=True, index=True)
    device_type = Column(String, nullable=True)
    onboarding_completed = Column(Boolean, default=False, nullable=False)
    onboarding_completed_at = Column(DateTime, nullable=True)
    assignment_zero_completed = Column(Boolean, default=False, nullable=False)
    assignment_zero_completed_at = Column(DateTime, nullable=True)
    student_id = Column(String, unique=True, nullable=True)
    # Stable central-IdP (Zitadel) subject, captured at OIDC login — the canonical
    # cross-system identity link (SSO Phase 2). Distinct from the overloaded student_id.
    central_auth_user_id = Column(String, nullable=True, index=True)
    total_study_time_minutes = Column(Integer, default=0, nullable=False)
    daily_streak = Column(Integer, default=0, nullable=False)
    last_activity_date = Column(Date, nullable=True)
    activity_points = Column(BigInteger, default=0, nullable=False)
    no_substitutions = Column(Boolean, default=False, nullable=False)
    is_analytics_hidden = Column(Boolean, default=False, nullable=False)

    groups = relationship("GroupStudent", back_populates="student", cascade="all, delete-orphan")
    enrollments = relationship("Enrollment", back_populates="user", cascade="all, delete-orphan")
    progress_records = relationship("StudentProgress", back_populates="user", cascade="all, delete-orphan")
    sent_messages = relationship("Message", foreign_keys="Message.from_user_id", back_populates="sender", cascade="all, delete-orphan")
    received_messages = relationship("Message", foreign_keys="Message.to_user_id", back_populates="recipient", cascade="all, delete-orphan")
    created_courses = relationship("Course", back_populates="teacher")
    assignment_submissions = relationship("AssignmentSubmission", foreign_keys="AssignmentSubmission.user_id", back_populates="user", cascade="all, delete-orphan")
    favorite_flashcards = relationship("FavoriteFlashcard", back_populates="user", cascade="all, delete-orphan", passive_deletes=True)
    step_progress = relationship("StepProgress", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    point_history = relationship("PointHistory", back_populates="user", cascade="all, delete-orphan")
    managed_courses = relationship("Course", secondary="course_head_teachers", back_populates="head_teachers")
    push_tokens = relationship("UserPushToken", back_populates="user", cascade="all, delete-orphan")
    # Parent ↔ student links (parent role / mobile parent portal).
    # children_links: rows where this user is the PARENT. parent_links: rows where this user is the STUDENT.
    children_links = relationship("ParentStudent", foreign_keys="ParentStudent.parent_id", back_populates="parent", cascade="all, delete-orphan")
    parent_links = relationship("ParentStudent", foreign_keys="ParentStudent.student_id", back_populates="student", cascade="all, delete-orphan")

    @property
    def course_ids(self) -> List[int]:
        return [c.id for c in self.managed_courses] if self.managed_courses else []


class PointHistory(Base):
    """Tracks every point transaction for leaderboard calculations."""
    __tablename__ = "point_history"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    user = relationship("UserInDB", back_populates="point_history")

    __table_args__ = (
        Index('ix_point_history_user_created', 'user_id', 'created_at'),
    )


class UserPushToken(Base):
    """Per-device Expo push token.

    Replaces the single ``users.push_token`` column (kept for backward
    compatibility) so one user can register multiple devices (e.g. web + phone).
    Tokens are globally unique; re-registering a device's token reassigns it to
    the current user via upsert.
    """
    __tablename__ = "user_push_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String, nullable=False, index=True)
    platform = Column(String, nullable=True)  # ios | android | web | expo
    device_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("UserInDB", back_populates="push_tokens")

    __table_args__ = (
        UniqueConstraint("token", name="uq_user_push_tokens_token"),
    )
