"""One definition of "a lesson this teacher is responsible for", and of "a payable lesson".

Every salary surface used to write its own version of this query and they disagreed. The
teacher dashboard promised

    «Пока посещаемость не отмечена, занятия … не попадают в расчёт зарплаты.»

while the endpoint behind it selected every completed lesson assigned to the teacher and
never looked at attendance at all. Unmarked lessons were therefore paid, which is the
opposite of what the screen said and of what the CRM's payroll did.

Two predicates live here and nothing else should re-implement them:

:func:`teacher_lesson_clause`
    *Responsibility.* The actual ``events.teacher_id`` — the person who stood in front of the
    lesson — falling back to the group's regular teacher only for legacy rows where the event
    never recorded one. This is what makes a substitution follow the substitute: for attendance
    duty *and* for pay, both of which must name the same person or one of them is wrong.

:func:`marked_attendance_clause`
    *Payability.* At least one attendance row carrying a status from the shared marked
    vocabulary (:mod:`src.services.attendance_status`) — the same set the CRM bills on, so a
    lesson cannot be payable in one system and unpaid in the other.

Deliberately NOT included here: exclusions (teacher/group/system). Those live in the CRM
database, which this process cannot read. The CRM applies them on top of this same predicate
via ``apply_event_exclusion_filters``; the LMS breakdown is therefore a teacher-facing preview
that can be equal to or slightly larger than the CRM's final figure, never smaller, and it
says so on the response. Making the two literally identical means moving exclusions into a
shared store, which is a bigger change than this repair.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, exists, func, or_

#: Kazakhstan is a single UTC+5 zone with no DST. Business dates are Almaty dates.
ALMATY = timezone(timedelta(hours=5))


def almaty_period_bounds(start_date, end_date) -> tuple[datetime, datetime]:
    """Inclusive Almaty calendar dates → naive-UTC half-open ``[start, end)``.

    A payroll period is a business fact, so its edges are Almaty midnights. Read as UTC those
    are 19:00 on the day before — five hours in which a UTC-bounded period puts a lesson in
    the wrong half of the month.
    """
    start_utc = (
        datetime(start_date.year, start_date.month, start_date.day, tzinfo=ALMATY)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    end_excl_local = (
        datetime(end_date.year, end_date.month, end_date.day, tzinfo=ALMATY) + timedelta(days=1)
    )
    end_utc = end_excl_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def teacher_lesson_clause(teacher_id: int):
    """Lessons ``teacher_id`` actually taught (or owes attendance for).

    The ``NULL`` fallback to the group's teacher covers lessons generated before events
    carried a teacher of their own. It is a fallback, not a parallel rule: once an event names
    a teacher, that name is the only one that counts — which is precisely what makes an
    approved substitution move both the attendance duty and the pay to the substitute.
    """
    from src.schemas.models import Event, Group

    return or_(
        Event.teacher_id == teacher_id,
        and_(Event.teacher_id.is_(None), Group.teacher_id == teacher_id),
    )


def marked_attendance_clause():
    """True for events with at least one canonically marked attendance row.

    Correlated ``EXISTS`` rather than a join so an event with thirty marked students is not
    counted thirty times, and so the clause composes with ``~`` for the "completed but
    unmarked" list without a second query shape.

    ``Attendance`` is the source of truth. ``EventParticipant`` is deliberately not consulted:
    it is deprecated for class attendance and its ``registration_status`` includes
    ``registered``, which means "enrolled", not "the register was taken".
    """
    from src.events.models import Attendance
    from src.schemas.models import Event
    from src.services.attendance_status import marked_statuses_for_sql

    return exists().where(
        and_(
            Attendance.event_id == Event.id,
            func.lower(func.coalesce(Attendance.status, "")).in_(marked_statuses_for_sql()),
        )
    )


def conducted_lessons_query(
    db,
    teacher_id: int,
    period_start_utc: datetime,
    period_end_utc: datetime,
    now_utc: Optional[datetime] = None,
):
    """Every completed, active class lesson this teacher taught in the period.

    The superset. Subtracting the payable set from it *is* the "completed but unmarked" list,
    so both have to come from one query or the two lists can disagree about their own total.

    ``end_datetime <= now`` is what makes it *completed*: a payslip lists work already done,
    and without this bound a breakdown generated mid-period bills lessons that have not
    happened yet.
    """
    from src.schemas.models import Event, EventGroup, Group

    now_utc = now_utc or datetime.utcnow()
    return (
        db.query(Event, Group)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .join(Group, Group.id == EventGroup.group_id)
        .filter(
            Event.event_type == "class",
            Event.is_active == True,  # noqa: E712  — cancelled lessons are never payable
            Event.start_datetime >= period_start_utc,
            Event.start_datetime < period_end_utc,
            Event.end_datetime <= now_utc,
            teacher_lesson_clause(teacher_id),
        )
    )


#: Why a completed lesson is not on the payable list, in the language the teacher reads.
OMISSION_REASONS = {
    "unmarked": "Посещаемость не отмечена — урок появится в расчёте после отметки",
    "cancelled": "Урок отменён",
    "not_finished": "Урок ещё не проведён",
    "excluded": "Урок исключён из расчёта",
}
