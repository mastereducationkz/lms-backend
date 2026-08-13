"""A read-only copy of the CRM's freeze lifecycle, kept for the LMS to display and count.

The CRM is canonical. Nothing here decides that a student is frozen; it records what the CRM
has decided, so that every LMS surface — rosters, attendance, the curator leaderboard, the
student's own banner — can show it without a network call per page. A live lookup would put
the CRM on the critical path of the LMS's busiest screens and would make a CRM outage look
like an LMS outage.

**The unit is one enrollment, not one student.** A student who freezes SAT while still
attending IELTS has one frozen scope and one running one, so every question asked here is
asked about a *group*: is this student frozen *on this group*, on this day. Keying the mirror
on the student alone is what emptied an IELTS attendance denominator because of a SAT freeze.
``group_id = 0`` is the sentinel for "student-wide": it is what every row written before
scoped freezes existed means, it is what an older CRM build still sends, and it keeps meaning
"every group". It is not nullable because a nullable column cannot be part of a primary key.

Idempotent by construction: an upsert keyed on (student, group), carrying the CRM's period id
and a monotonic revision. A retried or reordered delivery converges rather than flapping,
which matters because the outbox delivers at-least-once.

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
#: Returned, but not yet placed in a group. Deliberately **not** frozen: the student is back
#: and expected to study, and marking them frozen would hide the one case somebody has to act
#: on — a student waiting for a placement nobody has made.
FREEZE_PENDING_PLACEMENT = "pending_placement"

#: Sentinel scope meaning "every group of this student".
GROUP_WIDE = 0


class StudentFreezeState(Base):
    """Current freeze state per (student, group), as last reported by the CRM."""

    __tablename__ = "student_freeze_state"

    #: The LMS user id. One row per *scope*, not per student: history lives in the CRM, and
    #: the LMS only ever needs "is this enrollment frozen right now, and until when".
    user_id = Column(Integer, primary_key=True, index=True)
    #: The frozen group, or :data:`GROUP_WIDE` for a student-wide freeze. Part of the primary
    #: key, so two products freeze independently and neither delivery overwrites the other.
    group_id = Column(Integer, primary_key=True, nullable=False, default=GROUP_WIDE)
    #: Snapshots of the group's name and product, taken by the CRM at freeze time because the
    #: membership they were read from is deleted moments later. Staff-only labels.
    scope_group_name = Column(String(500), nullable=True)
    scope_product = Column(String(128), nullable=True)
    #: 24, matching the CRM's own column. ``pending_placement`` is seventeen characters, so
    #: the original 16 rejected the first delivery of the new status outright.
    status = Column(String(24), nullable=False, default=FREEZE_ACTIVE)
    freeze_start = Column(Date, nullable=True)
    planned_resume_date = Column(Date, nullable=True)
    actual_resume_date = Column(Date, nullable=True)
    reason_code = Column(String(32), nullable=True)
    responsible_curator_id = Column(Integer, nullable=True, index=True)
    crm_freeze_period_id = Column(Integer, nullable=True)
    #: Monotonic per (student, group). An older delivery arriving late is dropped rather than
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
    # A payload with no group_id comes from a CRM build that predates scoped freezes. It maps
    # onto the student-wide sentinel, so those deliveries keep applying to everything and the
    # two CRM versions can run side by side through the rollout.
    group_id = int(payload.get("group_id") or GROUP_WIDE)
    revision = int(payload.get("revision") or 0)

    row = (
        db.query(StudentFreezeState)
        .filter(
            StudentFreezeState.user_id == user_id,
            StudentFreezeState.group_id == group_id,
        )
        .first()
    )
    # Compared per scope, never per student: the CRM counts the revision on the freeze period,
    # so a SAT period's number says nothing about the IELTS one. Comparing across scopes would
    # let a stale SAT delivery silently discard a fresh IELTS update.
    if row is not None and revision < row.revision:
        return {"applied": False, "reason": "stale revision", "current_revision": row.revision}
    if row is None:
        row = StudentFreezeState(user_id=user_id, group_id=group_id)
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
    # Overwritten only when the payload carries them. An older CRM build sends a freeze with
    # no labels at all, and blanking «SAT · SAT-1» would leave staff a badge naming nothing.
    if payload.get("scope_group_name") is not None:
        row.scope_group_name = payload.get("scope_group_name")
    if payload.get("scope_product") is not None:
        row.scope_product = payload.get("scope_product")
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "applied": True,
        "lms_student_id": user_id,
        "group_id": group_id,
        "is_frozen": row.is_frozen,
    }


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class FreezeIndex:
    """All live freeze rows for a page, answering the scoped question.

    The scoped question — "is this student frozen *on this group*, on this day" — is asked
    once per cell of the curator leaderboard grid. Building the index once per request keeps
    that a dict lookup; asking the database per cell is an N+1 on the busiest curator screen
    there is.
    """

    __slots__ = ("_by_student",)

    def __init__(self, rows: Iterable[StudentFreezeState]) -> None:
        by_student: dict[int, list[StudentFreezeState]] = {}
        for row in rows:
            by_student.setdefault(int(row.user_id), []).append(row)
        for bucket in by_student.values():
            # Scoped rows first, student-wide last, so a caller wanting the most specific
            # answer can take the first match.
            bucket.sort(key=lambda r: (_scope(r) == GROUP_WIDE, _scope(r)))
        self._by_student = by_student

    def rows_for(self, user_id: int) -> list[StudentFreezeState]:
        return self._by_student.get(int(user_id), [])

    def _matching(self, user_id: int, group_id: Optional[int]) -> list[StudentFreezeState]:
        """Rows that speak about this group, most specific first.

        ``group_id=None`` asks the wider question "any scope at all", which is what the
        student's own banner needs — they are entitled to know they are frozen somewhere
        without the LMS having to name which product.
        """
        rows = self.rows_for(user_id)
        if group_id is None:
            return rows
        gid = int(group_id)
        return [row for row in rows if _scope(row) in (GROUP_WIDE, gid)]

    def is_frozen_on(
        self, user_id: int, group_id: Optional[int], day: Optional[date]
    ) -> bool:
        """Was this student's enrollment in this group frozen on this day?

        The whole feature in one method: a SAT freeze must leave an IELTS lesson in the
        denominator. The date rules stay in :func:`is_within_freeze` and are applied per
        matching row, so a legacy student-wide row and a scoped one each give exactly the
        answer they would give alone.
        """
        return any(is_within_freeze(row, day) for row in self._matching(user_id, group_id))

    def badge_for(
        self, user_id: int, group_id: Optional[int] = None, *, for_student: bool = False
    ) -> Optional[dict[str, Any]]:
        """What to render for this student, optionally narrowed to one group."""
        frozen = [row for row in self._matching(user_id, group_id) if row.is_frozen]
        if not frozen:
            return None
        row = frozen[0] if group_id is not None else _longest_running(frozen)
        badge = frozen_badge(row, for_student=for_student)
        if badge is None or for_student:
            # Students never read the scope labels. «SAT-1» is an internal name, and the
            # asymmetry with the reason code is deliberate — see :func:`frozen_badge`.
            return badge
        badge.update(
            {
                "group_id": _scope(row),
                "scope_group_name": row.scope_group_name,
                "scope_product": row.scope_product,
            }
        )
        return badge


def freeze_index(db: Session, user_ids: Iterable[int]) -> FreezeIndex:
    """Every freeze row for a page, in one query. The canonical read primitive."""
    ids = sorted({int(u) for u in user_ids})
    if not ids:
        return FreezeIndex([])
    return FreezeIndex(
        db.query(StudentFreezeState).filter(StudentFreezeState.user_id.in_(ids)).all()
    )


def _scope(row: StudentFreezeState) -> int:
    return int(row.group_id or GROUP_WIDE)


def _longest_running(rows: list[StudentFreezeState]) -> StudentFreezeState:
    """The freeze that ends last, for a badge that has to speak for the whole student.

    A student frozen on SAT until September and on IELTS until October is not back in
    September. Showing the earlier date would tell them they had returned while half their
    study was still suspended; an open-ended freeze outranks every dated one.
    """
    return max(rows, key=lambda r: (r.planned_resume_date or date.max, _scope(r)))


def freeze_states(db: Session, user_ids: Iterable[int]) -> dict[int, StudentFreezeState]:
    """Batched lookup returning **one representative row per student**.

    Kept for callers that ask the old student-wide question. It cannot answer "frozen on
    *this* group" — with one freeze per (student, group) a student can hold two rows that
    legitimately disagree — so every scoped caller must use :func:`freeze_index` instead.

    The representative is the row a student-wide badge would show: the live freeze that runs
    longest, or an arbitrary row when nothing is frozen.
    """
    ids = sorted({int(u) for u in user_ids})
    if not ids:
        return {}
    index = freeze_index(db, ids)
    out: dict[int, StudentFreezeState] = {}
    for user_id in ids:
        rows = index.rows_for(user_id)
        if not rows:
            continue
        frozen = [row for row in rows if row.is_frozen]
        out[user_id] = _longest_running(frozen) if frozen else rows[0]
    return out


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
    """Was this day inside the freeze this row describes?

    Used to drop lessons from attendance and homework denominators. Weeks *before* the freeze
    are untouched — a freeze must not retroactively rewrite a term the student actually
    studied — and a confirmed return re-opens counting from the actual resumption date, not
    from the date that had merely been planned. A row awaiting placement has already returned,
    so it is bounded by that same actual date.
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
