from sqlalchemy import Column, String, Integer, Float, DateTime, Date, Boolean, ForeignKey, Text, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from datetime import datetime, date, timezone

from src.models.base import Base


class StudentProgress(Base):
    __tablename__ = "student_progress"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    lesson_id = Column(Integer, ForeignKey("lessons.id"), nullable=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=True)
    status = Column(String, nullable=False, default="not_started")
    completion_percentage = Column(Integer, default=0)
    time_spent_minutes = Column(Integer, default=0)
    last_accessed = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)

    user = relationship("UserInDB", back_populates="progress_records")
    course = relationship("Course")
    lesson = relationship("Lesson")
    assignment = relationship("Assignment")


class StepProgress(Base):
    __tablename__ = "step_progress"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False)
    step_id = Column(Integer, ForeignKey("steps.id", ondelete="CASCADE"), nullable=False)
    status = Column(String, nullable=False, default="not_started")
    started_at = Column(DateTime, nullable=True)
    visited_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    time_spent_minutes = Column(Integer, default=0)

    user = relationship("UserInDB")
    course = relationship("Course")
    lesson = relationship("Lesson")
    step = relationship("Step")

    __table_args__ = (
        UniqueConstraint('user_id', 'step_id', name='uq_user_step_progress'),
        Index('idx_step_progress_course_status_completed', 'course_id', 'status', 'completed_at'),
        Index('idx_step_progress_user_course_status', 'user_id', 'course_id', 'status'),
        Index('idx_step_progress_user_visited', 'user_id', 'visited_at'),
    )


class ProgressSnapshot(Base):
    __tablename__ = "progress_snapshots"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=True)
    snapshot_date = Column(Date, nullable=False, default=date.today)
    completed_steps = Column(Integer, default=0, nullable=False)
    total_steps = Column(Integer, default=0, nullable=False)
    completion_percentage = Column(Float, default=0.0, nullable=False)
    total_time_spent_minutes = Column(Integer, default=0, nullable=False)
    assignments_completed = Column(Integer, default=0, nullable=False)
    total_assignments = Column(Integer, default=0, nullable=False)
    assignment_score_percentage = Column(Float, default=0.0, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("UserInDB")
    course = relationship("Course")

    __table_args__ = (
        UniqueConstraint('user_id', 'course_id', 'snapshot_date', name='uq_progress_snapshot'),
    )


class StudentCourseSummary(Base):
    __tablename__ = "student_course_summaries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    total_steps = Column(Integer, default=0)
    completed_steps = Column(Integer, default=0)
    completion_percentage = Column(Float, default=0.0)
    total_time_spent_minutes = Column(Integer, default=0)
    total_assignments = Column(Integer, default=0)
    completed_assignments = Column(Integer, default=0)
    total_assignment_score = Column(Float, default=0.0)
    max_possible_score = Column(Float, default=0.0)
    average_assignment_percentage = Column(Float, default=0.0)
    last_activity_at = Column(DateTime, nullable=True)
    last_lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="SET NULL"), nullable=True)
    last_lesson_title = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("UserInDB")
    course = relationship("Course")
    last_lesson = relationship("Lesson")

    __table_args__ = (
        UniqueConstraint('user_id', 'course_id', name='uq_user_course_summary'),
        Index('idx_user_course_summary', 'user_id', 'course_id'),
    )


class CourseAnalyticsCache(Base):
    __tablename__ = "course_analytics_cache"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), unique=True, nullable=False)
    total_enrolled = Column(Integer, default=0)
    active_students_7d = Column(Integer, default=0)
    active_students_30d = Column(Integer, default=0)
    average_completion_percentage = Column(Float, default=0.0)
    average_assignment_score = Column(Float, default=0.0)
    total_modules = Column(Integer, default=0)
    total_lessons = Column(Integer, default=0)
    total_steps = Column(Integer, default=0)
    total_assignments = Column(Integer, default=0)
    last_calculated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    course = relationship("Course")


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("steps.id", ondelete="CASCADE"), nullable=False, index=True)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False)
    quiz_title = Column(String, nullable=True)
    total_questions = Column(Integer, nullable=False)
    correct_answers = Column(Integer, nullable=False)
    score_percentage = Column(Float, nullable=False)
    answers = Column(Text, nullable=True)
    time_spent_seconds = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=True, onupdate=lambda: datetime.now(timezone.utc))
    is_draft = Column(Boolean, default=False, nullable=False)
    current_question_index = Column(Integer, default=0, nullable=True)
    quiz_content_hash = Column(String(64), nullable=True)
    is_graded = Column(Boolean, default=True)
    feedback = Column(Text, nullable=True)
    graded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    graded_at = Column(DateTime, nullable=True)

    user = relationship("UserInDB", foreign_keys=[user_id])
    step = relationship("Step")
    course = relationship("Course")
    lesson = relationship("Lesson")
    grader = relationship("UserInDB", foreign_keys=[graded_by])
