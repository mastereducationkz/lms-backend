from sqlalchemy import Column, String, Integer, Date, DateTime, Boolean, ForeignKey, Text, JSON, Index, UniqueConstraint, text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from src.models.base import Base


class CuratorTaskTemplate(Base):
    """
    Template defining a type of curator task.
    Admin creates templates; the scheduler generates instances from them.
    """
    __tablename__ = "curator_task_templates"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    task_type = Column(String, nullable=False)
    scope = Column(String, nullable=False, default="student")
    recurrence_rule = Column(JSON, nullable=True)
    deadline_rule = Column(JSON, nullable=True)
    order_index = Column(Integer, default=0)
    applicable_from_week = Column(Integer, nullable=True)
    applicable_to_week = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    instances = relationship("CuratorTaskInstance", back_populates="template",
                             cascade="all, delete-orphan")


class CuratorTaskInstance(Base):
    """
    A concrete task instance assigned to a curator.
    Created by scheduler (for recurring) or triggered by events (onboarding, renewal).
    """
    __tablename__ = "curator_task_instances"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(Integer, ForeignKey("curator_task_templates.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    curator_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)
    status = Column(String, nullable=False, default="pending")
    due_date = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    result_text = Column(Text, nullable=True)
    screenshot_url = Column(String, nullable=True)
    week_reference = Column(String, nullable=True)
    program_week = Column(Integer, nullable=True)
    custom_title = Column(String, nullable=True)
    #: Durable identity for tasks created by another service, e.g. ``freeze_return:{id}``.
    #:
    #: The CRM owns the freeze lifecycle and the LMS owns the task list, so "has this task
    #: already been created?" is a question asked across a network boundary that may retry.
    #: A name or a (curator, student, date) tuple is not an identity — the date can change and
    #: the curator can be reassigned. A unique key supplied by the originator is, and the
    #: uniqueness is enforced by the database rather than by the caller remembering to check.
    source_key = Column(String(128), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    template = relationship("CuratorTaskTemplate", back_populates="instances")
    curator = relationship("UserInDB", foreign_keys=[curator_id])
    student = relationship("UserInDB", foreign_keys=[student_id])
    group = relationship("Group", foreign_keys=[group_id])

    __table_args__ = (
        Index('ix_curator_task_instances_curator_status', 'curator_id', 'status'),
        Index('ix_curator_task_instances_week', 'week_reference'),
    )


class CuratorOnboarding(Base):
    """One onboarding **cycle** per (curator, student) pairing.

    Rows are created/retired by the onboarding reconciler based on the live
    (curator <-> student) relationship, NOT on account age.

    Lifecycle
    ---------
    The original design allowed exactly one row per pair for all time
    (``uq_curator_onboarding_pair``), which meant a student returning to a curator they had
    already been onboarded by could never get a fresh card — the reconciler either revived a
    historical ``cancelled`` row (losing the fact that a *previous* relationship had ended) or
    silently did nothing because a ``done`` row was in the way.

    A row is now a *cycle*: it is **open** while ``ended_at IS NULL`` and closed once the
    relationship ends. A pair may accumulate any number of closed cycles (``cycle_no`` 1, 2,
    3…) but only ever one open cycle, enforced by the partial unique index
    ``uq_curator_onboarding_active`` (Postgres) — see the Alembic revision. ``status``
    continues to mean what it always did (new|in_progress|done|cancelled), so every existing
    reader keeps working; ``ended_at`` is the new axis and defaults to NULL, which is exactly
    what every pre-existing row is: still open.
    """
    __tablename__ = "curator_onboarding"

    id = Column(Integer, primary_key=True, index=True)
    curator_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="SET NULL"),
                      nullable=True)
    status = Column(String, nullable=False, default="new")  # new|in_progress|done|cancelled
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    completed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                          nullable=True)

    # --- cycle metadata (see class docstring) ---
    # 1 for every pre-existing row; incremented when a pair starts a fresh relationship.
    cycle_no = Column(Integer, nullable=False, default=1, server_default="1")
    # NULL == open. Set when responsibility for this student leaves this curator.
    ended_at = Column(DateTime, nullable=True)
    # Why the cycle closed: relationship_ended | transferred_out | curator_deactivated |
    # legacy_cancelled | manual.
    end_reason = Column(String(64), nullable=True)

    # --- operational fields the CRM workspace edits ---
    # When ``status`` last moved. Drives the "in_progress overdue after N days without an
    # update" rule, which cannot use updated_at (any write touches that).
    status_changed_at = Column(DateTime, nullable=True)
    # Curator's planned next contact. Date, not datetime: the school plans in days.
    next_action_at = Column(Date, nullable=True)
    next_action_note = Column(String(500), nullable=True)

    curator = relationship("UserInDB", foreign_keys=[curator_id])
    student = relationship("UserInDB", foreign_keys=[student_id])
    group = relationship("Group", foreign_keys=[group_id])
    events = relationship(
        "CuratorOnboardingEvent",
        back_populates="onboarding",
        cascade="all, delete-orphan",
        order_by="CuratorOnboardingEvent.created_at",
    )
    notes = relationship(
        "CuratorOnboardingNote",
        back_populates="onboarding",
        cascade="all, delete-orphan",
        order_by="CuratorOnboardingNote.created_at",
    )

    __table_args__ = (
        # The cycle invariant, enforced by the database rather than by application timing:
        # any number of closed cycles per pair, at most one open one. Two racing
        # reconcilers therefore cannot both create a card — the loser gets an
        # IntegrityError and adopts the winner's row.
        Index(
            "uq_curator_onboarding_active",
            "curator_id",
            "student_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
        Index("ix_curator_onboarding_curator_status", "curator_id", "status"),
        Index("ix_curator_onboarding_student_open", "student_id", "ended_at"),
        Index("ix_curator_onboarding_next_action", "next_action_at"),
    )


class CuratorOnboardingEvent(Base):
    """Append-only history of everything that happened to one onboarding cycle.

    Nothing updates or deletes a row; a correction is a new row. Mirrors the CRM's
    ``audit_log`` shape (actor / action / before / after) so the two feeds can be merged in
    the UI without translation. ``actor_role`` is denormalised because it answers the
    question the board actually asks — "did a head curator intervene here?" — without a join
    to a role that may since have changed.
    """
    __tablename__ = "curator_onboarding_events"

    id = Column(Integer, primary_key=True, index=True)
    onboarding_id = Column(Integer, ForeignKey("curator_onboarding.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_name = Column(String(500), nullable=True)
    actor_role = Column(String(32), nullable=True)
    # cycle.opened | cycle.closed | status.changed | note.added | next_action.set |
    # group.changed | intervention
    action = Column(String(64), nullable=False)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    onboarding = relationship("CuratorOnboarding", back_populates="events")

    __table_args__ = (
        Index("ix_curator_onboarding_events_card_time", "onboarding_id", "created_at"),
    )


class CuratorOnboardingNote(Base):
    """A curator's private note on one onboarding cycle.

    Deliberately scoped to the *cycle*, not the student: two curators sharing a student each
    keep their own thread, and neither sees the other's. Head curators see both — they are
    the oversight role — which is enforced in the service layer, not here.
    """
    __tablename__ = "curator_onboarding_notes"

    id = Column(Integer, primary_key=True, index=True)
    onboarding_id = Column(Integer, ForeignKey("curator_onboarding.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_name = Column(String(500), nullable=True)
    author_role = Column(String(32), nullable=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    onboarding = relationship("CuratorOnboarding", back_populates="notes")

    __table_args__ = (
        Index("ix_curator_onboarding_notes_card_time", "onboarding_id", "created_at"),
    )
