from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class LessonRequest(Base):
    """Model for lesson substitution and reschedule requests."""
    __tablename__ = "lesson_requests"

    id = Column(Integer, primary_key=True, index=True)
    request_type = Column(String, nullable=False)
    status = Column(String, default="pending", nullable=False)
    requester_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    lesson_schedule_id = Column(Integer, ForeignKey("lesson_schedules.id", ondelete="SET NULL"), nullable=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    original_datetime = Column(DateTime, nullable=False)
    substitute_teacher_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    substitute_teacher_ids = Column(Text, nullable=True)
    confirmed_teacher_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    new_datetime = Column(DateTime, nullable=True)
    reason = Column(Text, nullable=True)
    admin_comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Cancel requests only. "cancel_only" | "add_replacement" (see
    # ``src.lesson_requests.services.CANCEL_RESOLUTIONS``). While the request is pending this
    # is the teacher's optional proposal; once approved it is the approver's decision.
    cancel_resolution = Column(String(32), nullable=True)
    # The lesson appended to the end of the course when the decision was "add_replacement".
    replacement_event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)

    requester = relationship("UserInDB", foreign_keys=[requester_id])
    substitute_teacher = relationship("UserInDB", foreign_keys=[substitute_teacher_id])
    confirmed_teacher = relationship("UserInDB", foreign_keys=[confirmed_teacher_id])
    resolver = relationship("UserInDB", foreign_keys=[resolved_by])
    lesson_schedule = relationship("LessonSchedule", foreign_keys=[lesson_schedule_id])
    event = relationship("Event", foreign_keys=[event_id])
    replacement_event = relationship("Event", foreign_keys=[replacement_event_id])
    group = relationship("Group", foreign_keys=[group_id])
