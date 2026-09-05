"""SAT Checkpoints: unit-gated 45-question assessments.

A CheckpointDefinition names the required units (lessons) of one block and the hidden lesson
whose quiz step carries the questions. A StudentCheckpoint is the per-student, per-group record
(status, opened_at, deadline, result). Rows are created lazily — a student without a row is
simply "locked".
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from src.models.base import Base

STATUS_LOCKED = "locked"
STATUS_AVAILABLE = "available"
STATUS_COMPLETED = "completed"
STATUS_OVERDUE = "overdue"
STATUS_REOPENED = "reopened"
ALL_STATUSES = (STATUS_LOCKED, STATUS_AVAILABLE, STATUS_COMPLETED, STATUS_OVERDUE, STATUS_REOPENED)
OPEN_STATUSES = (STATUS_AVAILABLE, STATUS_REOPENED)
UNIT_KINDS = ("verbal", "math")
DEFAULT_TOTAL_QUESTIONS = 45


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CheckpointDefinition(Base):
    __tablename__ = "checkpoint_definitions"
    id = Column(Integer, primary_key=True, index=True)
    # The course whose units gate this checkpoint (the SAT course), NOT the hidden quiz course.
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    title = Column(String, nullable=False)
    # Hidden lesson (in the "SAT Checkpoints" course) holding the single quiz step with the questions.
    quiz_lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="SET NULL"), nullable=True)
    total_questions = Column(Integer, nullable=False, default=DEFAULT_TOTAL_QUESTIONS,
                             server_default=str(DEFAULT_TOTAL_QUESTIONS))
    is_active = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    required_units = relationship(
        "CheckpointRequiredUnit", cascade="all, delete-orphan", passive_deletes=True,
        order_by="CheckpointRequiredUnit.position", lazy="selectin",
    )
    quiz_lesson = relationship("Lesson", foreign_keys=[quiz_lesson_id])

    __table_args__ = (UniqueConstraint("course_id", "number", name="uq_checkpoint_course_number"),)


class CheckpointRequiredUnit(Base):
    __tablename__ = "checkpoint_required_units"
    id = Column(Integer, primary_key=True, index=True)
    checkpoint_id = Column(Integer, ForeignKey("checkpoint_definitions.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # verbal | math
    position = Column(Integer, nullable=False, default=0)

    lesson = relationship("Lesson")

    __table_args__ = (UniqueConstraint("checkpoint_id", "lesson_id", name="uq_checkpoint_required_unit"),)


class StudentCheckpoint(Base):
    __tablename__ = "student_checkpoints"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    checkpoint_id = Column(Integer, ForeignKey("checkpoint_definitions.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    checkpoint_number = Column(Integer, nullable=False)
    # Snapshot of the required lesson ids at open time (ТЗ §12 "required unit IDs").
    required_unit_ids = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    status = Column(String(16), nullable=False, default=STATUS_LOCKED, server_default=STATUS_LOCKED)
    opened_at = Column(DateTime, nullable=True)
    deadline = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    quiz_attempt_id = Column(Integer, ForeignKey("quiz_attempts.id", ondelete="SET NULL"), nullable=True)
    correct_answers = Column(Integer, nullable=True)
    total_questions = Column(Integer, nullable=True)
    percentage = Column(Float, nullable=True)
    opened_by = Column(String(16), nullable=True)  # auto | admin
    reopen_count = Column(Integer, nullable=False, default=0, server_default="0")
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    definition = relationship("CheckpointDefinition")
    student = relationship("UserInDB", foreign_keys=[student_id])
    group = relationship("Group")
    quiz_attempt = relationship("QuizAttempt")

    __table_args__ = (
        UniqueConstraint("student_id", "group_id", "checkpoint_id", name="uq_student_checkpoint"),
        Index("ix_student_checkpoints_group_checkpoint", "group_id", "checkpoint_id"),
    )
