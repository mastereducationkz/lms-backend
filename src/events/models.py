from sqlalchemy import Column, String, Integer, Float, DateTime, Date, Boolean, ForeignKey, Text, UniqueConstraint, Index, CheckConstraint, func, text
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
    # server_default backstops direct (non-ORM) inserts — e.g. CRM writing this table
    # directly — so timestamps are never left NULL and the calendar serializer can't 500.
    created_at = Column(DateTime, server_default=func.now(), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, server_default=func.now(), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

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


class LessonRecording(Base):
    """One Google Meet recording, bound to the lesson it came from.

    The binding is explicit, never inferred. Google tells us a recording exists via
    ``conferenceRecords``; we resolve that to a Drive file id and match it back to the
    lesson through the Meet space we created for that lesson (``Event.meeting_url``).
    Timestamp- or filename-matching was considered and rejected — back-to-back lessons
    and rescheduled events make it guesswork (spec §4.3).

    `status` is payroll data, not just plumbing: accountants read it to decide whether a
    lesson gets paid ("no recording, no pay"), so the vocabulary is fixed and small.
      pending  — lesson happened, we are waiting for / working on the recording
      ready    — hls_url is populated and a student can watch it
      failed   — we found a recording but could not ingest it; needs a human
      missing  — the lesson ended and no recording ever arrived (the payroll case)
    """

    __tablename__ = "lesson_recordings"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)

    # Google's identifiers. conference_record is "conferenceRecords/<id>"; drive_file_id
    # is the file in the robot's Drive that Meet produced.
    conference_record = Column(String, nullable=True)
    drive_file_id = Column(String, nullable=True)
    # The copy we made into the Shared Drive. Kept separate from drive_file_id because
    # the original is deleted after 7 days (§4.6) while this one is the archive.
    shared_drive_file_id = Column(String, nullable=True)

    status = Column(String, nullable=False, default="pending")
    hls_url = Column(String, nullable=True)
    # The preview the Recordings library shows, stored beside the HLS so the same signed
    # token covers it. NULL for recordings ingested before previews existed.
    poster_url = Column(String, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, server_default=func.now(),
                        default=lambda: datetime.now(timezone.utc), nullable=False)
    ingested_at = Column(DateTime, nullable=True)
    # When the 7-day original was deleted from the robot's Drive. NULL means either not
    # yet due or not yet purged; the retention job uses it to stay idempotent.
    drive_purged_at = Column(DateTime, nullable=True)

    event = relationship("Event")

    __table_args__ = (
        # One recording row per lesson. Pub/Sub is gone but polling is still at-least-once:
        # the same conference will be seen on every tick until it reaches a terminal state,
        # and this constraint is what stops that becoming duplicate rows.
        UniqueConstraint("event_id", name="uq_lesson_recording_event"),
        # A Drive file must never be ingested twice, even if two lessons somehow resolve
        # to it. Partial: many rows legitimately sit with drive_file_id NULL while pending.
        Index("uq_lesson_recording_drive_file", "drive_file_id", unique=True,
              postgresql_where=text("drive_file_id IS NOT NULL")),
        Index("ix_lesson_recording_status", "status"),
    )


class MissingRecordingLog(Base):
    """A lesson that ended without a recording — raised before payroll runs.

    Deliberately shaped like MissedAttendanceLog (same idea: something a teacher was
    supposed to do and didn't), so accountants and curators read one familiar pattern
    rather than two. Resolvable, because a recording can still arrive late.
    """

    __tablename__ = "missing_recording_logs"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    detected_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    event = relationship("Event")
    teacher = relationship("UserInDB")

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_missing_recording_event"),
        Index("ix_missing_recording_teacher", "teacher_id"),
        Index("ix_missing_recording_resolved", "resolved_at"),
    )


class RecordingWatchLink(Base):
    """A login-free link to one lesson's recording, issued through the CRM.

    Accountants check recordings in the CRM and have no LMS account. The CRM decides who may
    watch (whoever sees the lesson on that screen) and asks for a link over its service
    channel; the link opens only this lesson, stops working after three hours, and every link
    records who asked for it and whether it was opened. Only a hash of the key is stored — the
    key itself lives in the URL and nowhere else.
    """

    __tablename__ = "recording_watch_links"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    issued_to = Column(String, nullable=True)  # the CRM user who asked, as the CRM names them
    issued_role = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
                        server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    first_opened_at = Column(DateTime, nullable=True)
    last_opened_at = Column(DateTime, nullable=True)
    open_count = Column(Integer, nullable=False, default=0, server_default="0")


class MeetConference(Base):
    """One call held in a lesson's Meet room.

    Every lesson has its own room, and a room opens a new call each time it goes from empty
    to occupied — an early test call, the lesson, a reconnect after everyone dropped. All of
    them belong to the lesson; which parts count is decided when the record is read, not here.
    Google keeps these for about 30 days, so this table is the lasting copy.
    """

    __tablename__ = "meet_conferences"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    conference_record = Column(String, nullable=False, unique=True)  # "conferenceRecords/<id>"
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    # Set once the call had ended and its people were saved; the job never asks again.
    synced_at = Column(DateTime, nullable=True)

    participants = relationship("MeetParticipant", back_populates="conference",
                                cascade="all, delete-orphan")


class MeetParticipant(Base):
    """One person in one call, as Meet identifies them.

    Meet gives a stable Google account id and a display name — never an email. Who that is in
    the LMS comes from GoogleAccountLink, confirmed once. A guest (not signed in) has no
    account to remember, so a guest is matched for this lesson only, on the row itself.
    """

    __tablename__ = "meet_participants"

    id = Column(Integer, primary_key=True, index=True)
    conference_id = Column(Integer, ForeignKey("meet_conferences.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_name = Column(String, nullable=False, unique=True)  # ".../participants/<id>"
    kind = Column(String, nullable=False)  # signed_in | guest | phone
    google_user = Column(String, nullable=True, index=True)  # "users/<id>", signed-in only
    display_name = Column(String, nullable=True)
    # Guests and phone callers only: who this was in this lesson, or not a student at all.
    lesson_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    lesson_not_a_student = Column(Boolean, nullable=False, default=False, server_default="false")

    conference = relationship("MeetConference", back_populates="participants")
    sessions = relationship("MeetParticipantSession", back_populates="participant",
                            cascade="all, delete-orphan", order_by="MeetParticipantSession.joined_at")


class MeetParticipantSession(Base):
    """One stretch in the call: a join and the leave that ended it. A reconnect is a new one."""

    __tablename__ = "meet_participant_sessions"

    id = Column(Integer, primary_key=True, index=True)
    participant_id = Column(Integer, ForeignKey("meet_participants.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    session_name = Column(String, nullable=False, unique=True)  # ".../participantSessions/<id>"
    joined_at = Column(DateTime, nullable=False)
    left_at = Column(DateTime, nullable=True)

    participant = relationship("MeetParticipant", back_populates="sessions")


class GoogleAccountLink(Base):
    """Which LMS person a Google account belongs to — confirmed once by a person, then used
    for every lesson, past and future.

    ``user_id`` NULL with ``not_a_student`` set means "someone we do not track here" (a staff
    member, a parent), so the account stops being asked about.
    """

    __tablename__ = "google_account_links"

    google_user = Column(String, primary_key=True)  # "users/<id>"
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    not_a_student = Column(Boolean, nullable=False, default=False, server_default="false")
    display_name = Column(String, nullable=True)  # as Meet showed it when confirmed
    confirmed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    confirmed_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
                          server_default=func.now())

    __table_args__ = (
        CheckConstraint("(user_id IS NOT NULL) <> not_a_student", name="ck_google_account_link_target"),
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
