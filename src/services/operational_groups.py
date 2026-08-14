"""Which groups belong on an operational calendar, and which lessons follow from them.

A group can be on the books for two quite different reasons, and the system kept answering
one question with the other:

* it is **operational** — somebody is expected to teach it this week, so it belongs on a
  calendar, in a filter, in a count, and in an actionable queue;
* it is **historical** — it ran, it has lessons, attendance and payslips, and all of that
  must stay readable forever.

*David Indi SAT - Нурай* (LMS group 79) is the case that named this module. It was switched
off, was not marked finished, and had no students enrolled at all — yet it still carried a
future event, so it appeared on the curator and head-curator calendars while the admin
calendar, which filtered differently, did not show it. Three surfaces, three definitions,
three different answers about the same group.

So there is one definition here and every operational surface asks it:

* ``is_active`` is the switch somebody flips when a group stops;
* ``is_over`` marks a finished course;
* and at least one *active* student is still enrolled — a register with nobody to call is not
  a task. Membership alone is not enough, because a group whose last member was deactivated
  still looks populated to a naive ``COUNT(*)`` over ``group_students``.

``is_over``/``is_special`` are NULL on legacy rows, so both are compared NULL-tolerantly.
``!= True`` alone would drop every legacy group and swing the bug the other way.

**This predicate never touches history.** It decides what to *show* and what to *ask somebody
to do*. An ineligible group keeps its lessons, its attendance, its audit trail and its
payroll exactly as they are — see :func:`event_has_operational_group_clause` for the one
subtlety that matters when an event is shared by several groups.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import and_, exists, or_


def group_is_operational_clause():
    """The group's own flags say it is still something a teacher is expected to run.

    Does **not** include the enrolment test — :func:`group_has_active_students_clause` is
    separate so a caller that has already joined ``group_students`` for its own reasons is not
    forced into a second correlated subquery. Most callers want
    :func:`operational_group_clause`, which is both.
    """
    from src.schemas.models import Group

    return and_(
        or_(Group.is_active == True, Group.is_active.is_(None)),  # noqa: E712
        or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
    )


def group_is_not_special_clause():
    """The group is ordinary teaching, not a special-programme container.

    `is_special` was read here as "test and administrative plumbing", and on that reading it
    belonged with the other two flags. Measuring which groups it actually removes said
    otherwise: in this database it names a *programme* — `NUET Special`,
    `10 grade - BIL Special`, `IELTS Special (Aigerim)` — carrying 30, 3 and 3 active students
    and 191 lessons between them.

    None of them has a teacher or a single attendance row. So they produce no attendance duty
    — nobody can be asked to mark a register for a lesson with no teacher — while their
    schedule remains a real schedule somebody's curator should be able to see.

    It therefore narrows *actionable* queues and never calendars.
    """
    from src.schemas.models import Group

    return or_(Group.is_special == False, Group.is_special.is_(None))  # noqa: E712


def group_has_active_students_clause():
    """At least one active student is still enrolled.

    The *person* has to still be active, not merely the membership row: the reported symptom
    came from a group holding one membership belonging to a deactivated account.
    """
    from src.schemas.models import Group, GroupStudent, UserInDB

    return exists().where(
        and_(
            GroupStudent.group_id == Group.id,
            GroupStudent.student_id == UserInDB.id,
            UserInDB.is_active == True,  # noqa: E712
        )
    )


def operational_group_clause():
    """On a calendar: the group is running and somebody is in it.

    Deliberately does not ask `is_special` — a special-programme group's schedule is real.
    """
    return and_(group_is_operational_clause(), group_has_active_students_clause())


def actionable_group_clause():
    """In a work queue: operational **and** somebody can actually be asked to act."""
    return and_(operational_group_clause(), group_is_not_special_clause())


def event_has_operational_group_clause():
    """True for an event attached to **at least one** operational group.

    This is the form a calendar wants, and it is deliberately not the same shape as joining
    ``Group`` and filtering. A lesson shared by two groups would come back twice from a join
    and appear twice on the calendar; as a correlated ``EXISTS`` over ``event_groups`` it is
    asked once per event and answered once.

    "At least one" is the rule and not "all": a lesson that a live group and a wound-up group
    both point at is still a lesson somebody has to teach. The caller applies its own
    authorization scope on top — this clause answers *is this operational*, never *may you
    see it*.
    """
    from src.schemas.models import Event, EventGroup, Group

    return exists().where(
        and_(
            EventGroup.event_id == Event.id,
            EventGroup.group_id == Group.id,
            operational_group_clause(),
        )
    )


def operational_group_ids(
    db, *, within: Optional[Iterable[int]] = None
) -> set[int]:
    """The ids of operational groups, optionally narrowed to a scope already computed.

    Materialised rather than correlated for the callers that need a set — filter dropdowns,
    counters, and the CRM-facing projections that must return the same list they filtered by.
    Pass ``within`` (an authorization scope) to keep the query small and to guarantee the
    result can never widen the caller's scope.

    An empty ``within`` means an empty scope, and therefore an empty result — never "all".
    """
    from src.schemas.models import Group

    if within is not None:
        within = list(within)
        if not within:
            return set()

    query = db.query(Group.id).filter(operational_group_clause())
    if within is not None:
        query = query.filter(Group.id.in_(within))
    return {row[0] for row in query.all()}
