"""When a lesson is allowed to nag somebody about attendance. One definition.

Gulzada saw «Attendance Required» for 11 unmarked lessons in *K_Aldiyar SAT 2026 - Gulzada*
— a group that had been switched off, with no active students left in it. There was nothing
she could do about it and nothing anybody wanted her to do about it; the lessons were simply
history nobody had closed the books on. The query behind the warning filtered on the lesson
(class, active, ended, past the launch cutoff) and never once asked whether the *group* was
still a going concern.

A warning is a request for action, so every condition here is really the same question asked
of a different object: **is there a person who can still do something about this lesson?**

* the lesson has ended, in Almaty, and is not cancelled — otherwise there is nothing to mark;
* the group is active, not finished, not a special/test group — otherwise nobody is running
  it any more;
* the group still has at least one active student — a register with nobody to call is not a
  task, and this is the condition that produced the reported symptom;
* attendance is genuinely still missing;
* and the person being asked is the lesson's *effective* teacher — the substitute after an
  approved replacement, never the group's owner.

The last one is why this module sits next to :mod:`src.services.lesson_teacher` and reuses
its clause rather than restating it: attendance duty and pay must name the same person, and
the way to guarantee that is for both to ask the same function.

Deliberately **not** included: the CRM's manual group-exclusion lists. They live in the CRM
database, which this process cannot read. The CRM applies them on top of this same predicate;
the LMS view is therefore equal to or slightly wider, never narrower, and the difference is
groups an administrator has hidden from *financial* reporting rather than from teaching.

Nothing here deletes or rewrites history. An ineligible lesson keeps its rows, its
attendance and its audit trail — it simply stops asking somebody to act on it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, exists, func, or_

#: Kazakhstan is a single UTC+5 zone with no DST.
ALMATY = timezone(timedelta(hours=5))

#: Lessons before the production launch are never chased. Kept here so the dashboard, the
#: totals and the detail list cannot disagree about where history begins.
ATTENDANCE_CUTOFF = datetime(2026, 2, 16, 0, 0, 0)


def group_is_operational_clause():
    """The group is still something a teacher is expected to run.

    ``is_active`` is the switch somebody flips when a group stops; ``is_over`` marks a
    finished course; ``is_special`` marks the test and administrative groups that exist for
    plumbing rather than teaching. None of the three produces work for a teacher, and the
    warning query used to ignore all of them.

    ``is_over``/``is_special`` are NULL on legacy rows, so both are compared with an explicit
    NULL-tolerant test — ``!= True`` alone drops every legacy group and would have swung the
    bug the other way.
    """
    from src.schemas.models import Group

    return and_(
        Group.is_active == True,  # noqa: E712
        or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        or_(Group.is_special == False, Group.is_special.is_(None)),  # noqa: E712
    )


def group_has_active_students_clause():
    """At least one active student is still enrolled.

    A register with nobody to call is not a task. This is the condition that produced the
    reported symptom: the group had one membership row left, belonging to a deactivated
    account, so it looked populated to a naive ``COUNT(*)`` over ``group_students``.
    Membership alone is not enough — the *person* has to still be active.
    """
    from src.schemas.models import Group, GroupStudent, UserInDB

    return exists().where(
        and_(
            GroupStudent.group_id == Group.id,
            GroupStudent.student_id == UserInDB.id,
            UserInDB.is_active == True,  # noqa: E712
        )
    )


def attendance_missing_clause():
    """No canonical attendance row exists for this lesson yet.

    Uses the shared marked-status vocabulary, so "marked" means the same thing to the
    warning, to the payslip and to the CRM.
    """
    from src.events.models import Attendance
    from src.schemas.models import Event
    from src.services.attendance_status import marked_statuses_for_sql

    return ~exists().where(
        and_(
            Attendance.event_id == Event.id,
            func.lower(func.coalesce(Attendance.status, "")).in_(marked_statuses_for_sql()),
        )
    )


def warning_eligible_events(
    db,
    *,
    teacher_id: Optional[int] = None,
    group_ids: Optional[list[int]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    now: Optional[datetime] = None,
    require_unmarked: bool = True,
):
    """The one query. Every warning surface must be built from this and nothing else.

    Returns a query of ``(Event, Group)`` for lessons that legitimately still need a register
    taken. ``teacher_id`` asks it as a duty — "what do *I* owe" — resolved through the
    effective-teacher rule so a substitute is asked and the group's owner is not.

    The dashboard card, the totals, the group count, the oldest-missing date, the detail
    list, the mobile view and any reminder derived from unmarked attendance all read from
    here, which is what makes the summary and the detail agree by construction rather than
    by two queries happening to match.
    """
    from src.schemas.models import Event, EventGroup, Group
    from src.services.payable_lessons import teacher_lesson_clause

    now = now or datetime.utcnow()
    query = (
        db.query(Event, Group)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .join(Group, Group.id == EventGroup.group_id)
        .filter(
            Event.event_type == "class",
            Event.is_active == True,  # noqa: E712  — a cancelled lesson needs no register
            Event.end_datetime <= now,  # it has actually happened
            Event.end_datetime >= ATTENDANCE_CUTOFF,
            group_is_operational_clause(),
            group_has_active_students_clause(),
        )
    )
    # "No register at all" is the coarse test. A caller that compares *expected* students
    # against *marked* ones — a lesson where one of two was registered is still incomplete —
    # does that comparison itself and passes False, so this must not pre-filter those rows
    # away. Eligibility above still applies either way, which is the part that was missing.
    if require_unmarked:
        query = query.filter(attendance_missing_clause())
    if teacher_id is not None:
        query = query.filter(teacher_lesson_clause(teacher_id))
    if group_ids is not None:
        if not group_ids:
            return query.filter(Event.id < 0)  # empty scope means empty result, not "all"
        query = query.filter(EventGroup.group_id.in_(group_ids))
    if start is not None:
        query = query.filter(Event.start_datetime >= start)
    if end is not None:
        query = query.filter(Event.start_datetime < end)
    return query


def warning_summary(db, *, teacher_id: int, now: Optional[datetime] = None) -> dict:
    """Counts for the dashboard card, drawn from the same rows as the list beneath it.

    The card and the list used to be two queries that agreed by coincidence. They are now one
    query read twice, so a mismatch is not expressible.
    """
    rows = warning_eligible_events(db, teacher_id=teacher_id, now=now).all()
    group_ids = {group.id for _event, group in rows}
    oldest = min((event.start_datetime for event, _g in rows), default=None)
    return {
        "lessons": len(rows),
        "groups": len(group_ids),
        "oldest_missing_date": oldest.isoformat() if oldest else None,
    }
