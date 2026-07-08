from sqlalchemy import Column, String, Integer, Float, DateTime, Date, Boolean, ForeignKey, Text, UniqueConstraint, Index, CheckConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    # Optional lesson topic/subject shown alongside the auto-generated "Group: Lesson N"
    # title (e.g. in the curator attendance view). Editable per class session.
    topic = Column(String, nullable=True)
    event_type = Column(String, nullable=False)
    start_datetime = Column(DateTime, nullable=False)
    end_datetime = Column(DateTime, nullable=False)
    location = Column(String, nullable=True)
    is_online = Column(Boolean, default=True)
    meeting_url = Column(String, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    is_recurring = Column(Boolean, default=False)
    recurrence_pattern = Column(String, nullable=True)
    recurrence_end_date = Column(Date, nullable=True)
    max_participants = Column(Integer, nullable=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    creator = relationship("UserInDB", foreign_keys=[created_by])
    event_groups = relationship("EventGroup", back_populates="event", cascade="all, delete-orphan")
    event_courses = relationship("EventCourse", back_populates="event", cascade="all, delete-orphan")
    event_participants = relationship("EventParticipant", back_populates="event", cascade="all, delete-orphan")
    teacher = relationship("UserInDB", foreign_keys=[teacher_id])

    @property
    def is_substitution(self):
        if self.teacher_id and self.event_groups:
            try:
                first_group_assoc = self.event_groups[0]
                if first_group_assoc.group and first_group_assoc.group.teacher_id:
                    return self.teacher_id != first_group_assoc.group.teacher_id
            except (IndexError, AttributeError):
                pass
        return False

    @property
    def teacher_name(self):
        return self.teacher.name if self.teacher else None


class EventGroup(Base):
    __tablename__ = "event_groups"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    event = relationship("Event", back_populates="event_groups")
    group = relationship("Group")

    __table_args__ = (
        UniqueConstraint('event_id', 'group_id', name='uq_event_group'),
    )


class EventCourse(Base):
    __tablename__ = "event_courses"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    event = relationship("Event", back_populates="event_courses")
    course = relationship("Course")

    __table_args__ = (
        UniqueConstraint('event_id', 'course_id', name='uq_event_course'),
    )


class EventParticipant(Base):
    """
    DEPRECATED for attendance tracking.
    Use Attendance (with event_id) as the single source of truth.
    EventParticipant may still be used for webinar/non-class event registration.
    """
    __tablename__ = "event_participants"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    registration_status = Column(String, default="registered")
    registered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    attended_at = Column(DateTime, nullable=True)
    activity_score = Column(Float, nullable=True)

    event = relationship("Event", back_populates="event_participants")
    user = relationship("UserInDB")

    __table_args__ = (
        UniqueConstraint('event_id', 'user_id', name='uq_event_participant'),
    )


class MissedAttendanceLog(Base):
    __tablename__ = "missed_attendance_logs"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    detected_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    expected_count = Column(Integer, nullable=False)
    recorded_count_at_detection = Column(Integer, default=0)
    resolved_at = Column(DateTime, nullable=True)
    resolved_count = Column(Integer, nullable=True)

    event = relationship("Event")
    group = relationship("Group")
    teacher = relationship("UserInDB")

    __table_args__ = (
        UniqueConstraint('event_id', 'group_id', name='uq_missed_attendance_event_group'),
        Index('ix_missed_attendance_teacher', 'teacher_id'),
        Index('ix_missed_attendance_resolved', 'resolved_at'),
    )


class LessonSchedule(Base):
    __tablename__ = "lesson_schedules"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    lesson_id = Column(Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    week_number = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint('group_id', 'scheduled_at', name='uq_lesson_schedule_group_time'),
    )

    group = relationship("Group", backref="lesson_schedules")
    lesson = relationship("Lesson")
    attendances = relationship("Attendance", back_populates="lesson_schedule", cascade="all, delete-orphan",
                               foreign_keys="Attendance.lesson_schedule_id")


class Attendance(Base):
    """
    Single source of truth for student attendance.

    Covers two lesson sources (exactly one must be set):
    - event_id: lesson created by Schedule Generator (current flow)
    - lesson_schedule_id: legacy LessonSchedule-based lesson

    EventParticipant is deprecated for attendance; use this model instead.
    """
    __tablename__ = "attendances"
    id = Column(Integer, primary_key=True, index=True)

    # Exactly one of the two below must be set (enforced by DB CHECK constraint)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True)
    lesson_schedule_id = Column(Integer, ForeignKey("lesson_schedules.id", ondelete="CASCADE"), nullable=True)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String, default="present")
    score = Column(Integer, default=0)
    activity_score = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    event = relationship("Event")
    lesson_schedule = relationship("LessonSchedule", back_populates="attendances",
                                   foreign_keys=[lesson_schedule_id])
    user = relationship("UserInDB")

    __table_args__ = (
        UniqueConstraint('event_id', 'user_id', name='uq_attendance_event_user'),
        CheckConstraint(
            '(event_id IS NOT NULL AND lesson_schedule_id IS NULL) OR '
            '(event_id IS NULL AND lesson_schedule_id IS NOT NULL)',
            name='ck_attendance_event_or_schedule'
        ),
    )
