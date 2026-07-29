from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class FavoriteFlashcard(Base):
    __tablename__ = "favorite_flashcards"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    step_id = Column(Integer, ForeignKey("steps.id", ondelete="CASCADE"), nullable=True)
    flashcard_id = Column(String, nullable=False)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=True)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=True)
    flashcard_data = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("UserInDB", back_populates="favorite_flashcards")
    step = relationship("Step", back_populates="favorite_flashcards")

    __table_args__ = (
        UniqueConstraint('user_id', 'step_id', 'flashcard_id', name='uq_user_flashcard'),
    )


class FavoriteStep(Base):
    __tablename__ = "favorite_steps"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("steps.id", ondelete="CASCADE"), nullable=False)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('user_id', 'step_id', name='uq_user_step'),
    )


class QuestionErrorReport(Base):
    """Model for tracking error reports submitted by users for quiz questions."""
    __tablename__ = "question_error_reports"

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(String(255), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    step_id = Column(Integer, ForeignKey("steps.id", ondelete="SET NULL"), nullable=True)
    message = Column(Text, nullable=False)
    suggested_answer = Column(Text, nullable=True)
    status = Column(String, default="pending", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("UserInDB", foreign_keys=[user_id])
    step = relationship("Step", foreign_keys=[step_id])
    resolver = relationship("UserInDB", foreign_keys=[resolved_by])
