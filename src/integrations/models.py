"""Storage for platform events (Platform Integration Pack §2.5)."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from src.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# JSONB on Postgres, plain JSON on SQLite (tests).
_JSONB = JSON().with_variant(JSONB(), "postgresql")


class PlatformEvent(Base):
    """Append-only copy of every event a platform pushed. ``(platform, event_id)`` is the
    idempotency key; ``user_id`` is the resolved LMS user (NULL + error='unresolved' when the
    student could not be matched yet — re-resolved nightly). Pruned after 400 days."""

    __tablename__ = "platform_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(16), nullable=False)
    event_id = Column(String(64), nullable=False)
    event_type = Column(String(64), nullable=False)
    occurred_at = Column(DateTime, nullable=False)  # naive UTC
    received_at = Column(DateTime, nullable=False, default=_utcnow)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    email = Column(String, nullable=True)
    zitadel_subject = Column(String(64), nullable=True)
    payload = Column(_JSONB, nullable=False)
    processed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)  # unresolved | unhandled_event_type | projection error

    __table_args__ = (
        UniqueConstraint("platform", "event_id", name="uq_platform_events_platform_event_id"),
        Index("ix_platform_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_platform_events_occurred_at", "occurred_at"),
        Index("ix_platform_events_error", "error"),
    )


class PlatformResult(Base):
    """Latest known state of one module attempt on a platform, keyed by
    ``(platform, module, attempt_ref)``. Status only ever moves forward:
    started → submitted|expired|completed → scored."""

    __tablename__ = "platform_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    platform = Column(String(16), nullable=False)
    track = Column(String(16), nullable=False)
    module = Column(String(16), nullable=False)
    test_id = Column(Integer, nullable=True)
    test_title = Column(String, nullable=True)
    attempt_ref = Column(String(64), nullable=False)
    weekly_set_id = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False)  # started|submitted|expired|completed|scored
    band = Column(Float, nullable=True)  # platform rounding, never re-derived here
    raw_score = Column(Integer, nullable=True)
    total = Column(Integer, nullable=True)
    result_url = Column(String, nullable=True)  # path on the platform; wrapped in a handoff link
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    scored_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("platform", "module", "attempt_ref", name="uq_platform_results_attempt"),
        Index("ix_platform_results_user_set", "user_id", "weekly_set_id"),
        Index("ix_platform_results_platform_set", "platform", "weekly_set_id"),
    )


class PlatformWeeklySet(Base):
    """A platform's weekly test set (title, window, modules) — drives E1/E2 later."""

    __tablename__ = "platform_weekly_sets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(16), nullable=False)
    weekly_set_id = Column(Integer, nullable=False)
    title = Column(String, nullable=True)
    # Full timestamps (naive UTC): the AI Speaking part closes at date_to's exact minute.
    date_from = Column(DateTime, nullable=True)
    date_to = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    track = Column(String(16), nullable=True)
    modules = Column(_JSONB, nullable=True)  # [{module, test_id, test_title}]
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("platform", "weekly_set_id", name="uq_platform_weekly_sets_set"),
        Index("ix_platform_weekly_sets_active_window", "platform", "is_active", "date_to"),
    )


class PlatformTestAssignment(Base):
    """Links one auto-created ``platform_test`` Assignment to its weekly set and group
    (Platform Integration Pack §6.3, E1). The assignment itself is a normal ``assignments`` row."""

    __tablename__ = "platform_test_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, unique=True)
    platform = Column(String(16), nullable=False)
    weekly_set_id = Column(Integer, nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("platform", "weekly_set_id", "group_id", name="uq_platform_test_assignments_set_group"),
        Index("ix_platform_test_assignments_group", "group_id"),
    )
