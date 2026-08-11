"""A read-only copy of the CRM's freeze lifecycle, kept for the LMS to display and count.

The CRM is canonical. Nothing here decides that a student is frozen; it records what the CRM
has decided, so that every LMS surface — rosters, attendance, the curator leaderboard, the
student's own banner — can show it without a network call per page. A live lookup would put
the CRM on the critical path of the LMS's busiest screens and would make a CRM outage look
like an LMS outage.

Idempotent by construction: an upsert keyed on the student, carrying the CRM's period id and
a monotonic revision. A retried or reordered delivery converges rather than flapping, which
matters because the outbox delivers at-least-once.

What the mirror deliberately does *not* do:

* it never changes LMS access — freeze is a study state, and platform access is a separate
  policy with its own rules and its own audit;
* it never exposes ``reason_note`` to students — the reason is staff information, and a
  student reading "финансовый вопрос" about themselves is a support incident.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from src.models import Base

#: Mirrors ``src.health.models`` in the CRM. Only ``active`` suppresses anything.
FREEZE_ACTIVE = "active"
FREEZE_RESUMED = "resumed"
FREEZE_CANCELLED = "cancelled"


class StudentFreezeState(Base):
    """Current freeze state per student, as last reported by the CRM."""

    __tablename__ = "student_freeze_state"

    #: The LMS user id. One row per student: history lives in the CRM, and the LMS only ever
    #: needs "is this student frozen right now, and until when".
    user_id = Column(Integer, primary_key=True, index=True)
    status = Column(String(16), nullable=False, default=FREEZE_ACTIVE)
    freeze_start = Column(Date, nullable=True)
    planned_resume_date = Column(Date, nullable=True)
    actual_resume_date = Column(Date, nullable=True)
    reason_code = Column(String(32), nullable=True)
    responsible_curator_id = Column(Integer, nullable=True, index=True)
    crm_freeze_period_id = Column(Integer, nullable=True)
    #: Monotonic per student. An older delivery arriving late is dropped rather than
    #: overwriting a newer state — at-least-once delivery does not promise order.
    revision = Column(Integer, nullable=False, default=0)
    is_frozen = Column(Boolean, nullable=False, default=True, index=True)
    note = Column(Text, nullable=True)
    updated_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


def upsert_freeze_state(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one CRM freeze update. Safe to replay."""
    user_id = payload.get("lms_student_id")
    if not user_id:
        return {"applied": False, "reason": "missing lms_student_id"}
    user_id = int(user_id)
    revision = int(payload.get("revision") or 0)

    row = db.query(StudentFreezeState).filter(StudentFreezeState.user_id == user_id).first()
    if row is not None and revision < row.revision:
        return {"applied": False, "reason": "stale revision", "current_revision": row.revision}
    if row is None:
        row = StudentFreezeState(user_id=user_id)
        db.add(row)

    status = (payload.get("status") or FREEZE_ACTIVE).strip().lower()
    row.status = status
    row.is_frozen = status == FREEZE_ACTIVE
    row.freeze_start = _parse_date(payload.get("freeze_start"))
    row.planned_resume_date = _parse_date(payload.get("planned_resume_date"))
    row.actual_resume_date = _parse_date(payload.get("actual_resume_date"))
    row.reason_code = payload.get("reason_code")
    row.responsible_curator_id = payload.get("responsible_curator_id")
    row.crm_freeze_period_id = payload.get("crm_freeze_period_id")
    row.revision = revision
    row.note = payload.get("note")
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return {"applied": True, "lms_student_id": user_id, "is_frozen": row.is_frozen}


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def freeze_states(db: Session, user_ids: Iterable[int]) -> dict[int, StudentFreezeState]:
    """Batched lookup. Every consumer is a list screen; none of them may query per row."""
    ids = sorted({int(u) for u in user_ids})
    if not ids:
        return {}
    return {
        row.user_id: row
        for row in db.query(StudentFreezeState)
        .filter(StudentFreezeState.user_id.in_(ids))
        .all()
    }


def frozen_badge(row: Optional[StudentFreezeState], *, for_student: bool = False) -> Optional[dict[str, Any]]:
    """What to render. Staff get the details; the student gets the date and nothing else.

    The asymmetry is deliberate. A student needs to know when they are expected back; they do
    not need to read the school's internal reason for their own freeze.
    """
    if row is None or not row.is_frozen:
        return None
    planned = row.planned_resume_date
    payload: dict[str, Any] = {
        "is_frozen": True,
        "planned_resume_date": planned.isoformat() if planned else None,
        "label": (
            f"Заморожен до {planned.strftime('%d.%m.%Y')}" if planned else "Заморожен"
        ),
    }
    if for_student:
        return payload
    payload.update(
        {
            "freeze_start": row.freeze_start.isoformat() if row.freeze_start else None,
            "reason_code": row.reason_code,
            "responsible_curator_id": row.responsible_curator_id,
            "is_overdue": bool(planned and planned < date.today()),
        }
    )
    return payload


def is_within_freeze(row: Optional[StudentFreezeState], day: Optional[date]) -> bool:
    """Was this day inside the student's freeze?

    Used to drop lessons from attendance and homework denominators. Weeks *before* the freeze
    are untouched — a freeze must not retroactively rewrite a term the student actually
    studied — and a confirmed return re-opens counting from the actual resumption date, not
    from the date that had been planned.
    """
    if row is None or day is None:
        return False
    start = row.freeze_start
    if start and day < start:
        return False
    if row.status == FREEZE_ACTIVE:
        return not start or day >= start
    end = row.actual_resume_date
    if end is None:
        return False
    return bool(start and start <= day < end)
