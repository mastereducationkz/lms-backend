"""The register stores decisions, not measurements.

Minutes late come from the Meet record and are recomputed on every read, so a call that Google
hands over late corrects an open period by itself. A row here means a person acted: confirmed a
fine, waived it with a reason, priced a miss, or closed a period for payroll.
"""
from datetime import datetime

from sqlalchemy import (JSON, Column, Date, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)

from src.models.base import Base


class DisciplineDecision(Base):
    """What a head teacher settled for one lesson: the amount owed, and why.

    One row per (lesson, teacher, kind): a lesson that started late *and* finished early owes for
    both, and each can be waived on its own. ``event_id`` is NULL for something Meet never saw.
    """

    __tablename__ = "discipline_decisions"
    __table_args__ = (UniqueConstraint("event_id", "teacher_id", "kind",
                                       name="uq_discipline_decision_lesson_kind"),)

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True)
    teacher_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)        # the Almaty day it belongs to
    kind = Column(String, nullable=False)                  # late | ended_early | miss
    minutes = Column(Integer, nullable=True)               # what the rule measured when decided
    proposed_amount = Column(Integer, nullable=True)       # what the rule asked for, in ₸
    amount = Column(Integer, nullable=False, default=0)    # what is owed; 0 means waived
    reason_code = Column(String, nullable=True)            # substitute | moved | technical | other
    note = Column(Text, nullable=True)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    decided_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class DisciplinePeriod(Base):
    """A half-month. Once closed it keeps its totals, because payroll was paid on them."""

    __tablename__ = "discipline_periods"

    id = Column(Integer, primary_key=True, index=True)
    period_key = Column(String, nullable=False, unique=True, index=True)  # "2026-09-16"
    starts_on = Column(Date, nullable=False)
    ends_on = Column(Date, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    closed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    totals = Column(JSON, nullable=True)                   # frozen at closing, for payroll


class DisciplineDigestSend(Base):
    """One row per day posted to «Штрафы учителя» — and the row is what claims the day.

    It is written before the network call, so two scheduler ticks racing cannot both post the
    same morning: the unique constraint decides which one owns it. A day nobody was fined ends
    as `skipped`, which records that we looked rather than that we failed.
    """

    __tablename__ = "discipline_digest_sends"
    __table_args__ = (UniqueConstraint("kind", "day", name="uq_discipline_digest_kind_day"),)

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(16), nullable=False, default="fines")
    day = Column(Date, nullable=False, index=True)         # the Almaty day it is about
    status = Column(String(16), nullable=False, default="pending")  # pending|sent|failed|skipped
    attempts = Column(Integer, nullable=False, default=0)
    telegram_message_id = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)
