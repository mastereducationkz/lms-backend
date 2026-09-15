"""Calendar subscriptions: a real Google Calendar per live group, and a personal ICS feed token.

Owner, 2026-09-15: the calendar a student subscribes to must match the actual schedule at all
times. Google refreshes a subscribed ICS URL only every 12–24 h, so each live group gets its own
Google Calendar owned by the robot account and kept in sync by the LMS; Apple/Outlook users take
the ICS feed; everyone can also take a personal feed of all their own lessons and deadlines.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from src.models.base import Base


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GroupGoogleCalendar(Base):
    """The robot-owned Google Calendar that mirrors one group's lessons, tests and deadlines."""

    __tablename__ = "group_google_calendars"

    id = Column(Integer, primary_key=True)
    lms_group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, unique=True)
    calendar_id = Column(String, nullable=False, unique=True)
    #: Readable by anyone with the link (ACL ``default`` reader) — set once the ACL call succeeded.
    public = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_now)
    #: Hash of the entries last written; an unchanged hash means nothing to send to Google.
    synced_hash = Column(String(64), nullable=True)
    synced_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    error_at = Column(DateTime, nullable=True)


class CalendarFeedToken(Base):
    """The secret in one person's personal ICS URL. Rotating it kills every old subscription."""

    __tablename__ = "calendar_feed_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    token = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    rotated_at = Column(DateTime, nullable=True)
