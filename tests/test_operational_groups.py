"""One definition of "this group belongs on a calendar", asked the way a calendar asks it.

The warning path already had these questions (see `test_attendance_warning_eligibility.py`).
What it did not have was the *event*-shaped form a calendar needs, and that is where the
reported bug lived: `David Indi SAT - Нурай` (production group 79) was switched off, was not
marked finished, and had no students enrolled at all — yet it carried a future event, so it
appeared on the curator and head-curator calendars while the admin calendar did not show it.

The multi-group test is the one that would be easy to get wrong in the obvious way. Filtering
by joining `Group` returns a shared lesson once per matching group, so a lesson two live
groups point at would be drawn twice on the calendar. `event_has_operational_group_clause`
is a correlated EXISTS for exactly that reason, and the assertion below is on the *count*.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.schemas.models import Event, EventGroup, Group, GroupStudent, UserInDB
from src.services.operational_groups import (
    event_has_operational_group_clause,
    operational_group_clause,
    operational_group_ids,
)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close()
        trans.rollback()


def _user(db, role="student", active=True):
    u = UserInDB(
        email=f"{role}-{datetime.utcnow().timestamp()}-{id(object())}@t.local",
        name=role, role=role, hashed_password="x", is_active=active,
    )
    db.add(u); db.flush()
    return u


@pytest.fixture
def world(db):
    teacher = _user(db, "teacher")

    def group(name="G", **flags):
        g = Group(name=name, teacher_id=teacher.id,
                  **{"is_active": True, "is_over": False, "is_special": False, **flags})
        db.add(g); db.flush()
        return g

    def enrol(group, *, active=True):
        student = _user(db, "student", active=active)
        db.add(GroupStudent(group_id=group.id, student_id=student.id,
                            created_at=datetime.utcnow() - timedelta(days=90)))
        db.flush()
        return student

    def lesson(*groups, days_ahead=1):
        start = datetime.utcnow() + timedelta(days=days_ahead)
        ev = Event(title="Урок", event_type="class", start_datetime=start,
                   end_datetime=start + timedelta(hours=1), is_active=True,
                   teacher_id=teacher.id, created_by=teacher.id)
        db.add(ev); db.flush()
        for g in groups:
            db.add(EventGroup(event_id=ev.id, group_id=g.id))
        db.flush()
        return ev

    return {"db": db, "teacher": teacher, "group": group, "enrol": enrol, "lesson": lesson}


def _operational(world):
    return operational_group_ids(world["db"])


def _calendar_event_ids(world):
    """What an operational calendar would draw, asked exactly as a calendar asks it."""
    return [
        row.id
        for row in world["db"].query(Event).filter(event_has_operational_group_clause()).all()
    ]


# --- the group predicate -------------------------------------------------------------------


def test_a_live_group_with_a_live_roster_is_operational(world):
    """The control. Without it every assertion below passes for an empty predicate."""
    group = world["group"]()
    world["enrol"](group)

    assert group.id in _operational(world)


@pytest.mark.parametrize("flags", [
    pytest.param({"is_active": False}, id="switched-off"),
    pytest.param({"is_over": True}, id="finished"),
])
def test_a_group_whose_flags_say_it_stopped_is_not_operational(world, flags):
    group = world["group"](**flags)
    world["enrol"](group)

    assert group.id not in _operational(world)


def test_a_group_whose_only_student_was_deactivated_is_not_operational(world):
    """Membership alone is not enough — the *person* has to still be active.

    The membership row survives, which is what made a naive COUNT(*) call this group full.
    """
    group = world["group"]()
    world["enrol"](group, active=False)

    assert group.id not in _operational(world)


def test_an_empty_group_is_not_operational(world):
    group = world["group"]()

    assert group.id not in _operational(world)


def test_legacy_null_flags_do_not_disqualify_a_group(world):
    """`!= True` alone would drop every legacy row and swing the bug the other way."""
    group = world["group"](is_over=None, is_special=None)
    world["enrol"](group)

    assert group.id in _operational(world)


def test_a_null_is_active_means_on(world):
    """The column is nullable and its model default is True, so NULL is "on".

    Written as a decision rather than left to fall out of `== True`, which would drop such a
    group from every calendar without anybody choosing that. Production has no NULLs today —
    this is here so it stays harmless if one appears.
    """
    group = world["group"](is_active=None)
    world["enrol"](group)

    assert group.id in _operational(world)


# --- special-programme groups ------------------------------------------------------------------
#
# `is_special` was read as "test and administrative plumbing" and used to hide groups from
# calendars. Measuring which groups it actually removes said otherwise: in production it names
# a *programme* — `NUET Special`, `10 grade - BIL Special`, `IELTS Special (Aigerim)` — with
# 30, 3 and 3 active students and 191 lessons between them. None has a teacher or a single
# attendance row.
#
# So it answers "can anybody be asked to act", not "is this real". It narrows queues, never
# calendars.


def test_a_special_group_is_still_on_the_calendar(world):
    """The regression this split fixes. Thirty students' schedule is not plumbing."""
    from src.services.operational_groups import actionable_group_clause

    group = world["group"](name="10 grade - BIL Special", is_special=True)
    world["enrol"](group)

    assert group.id in _operational(world)


def test_a_special_group_is_not_in_an_actionable_queue(world):
    """No teacher, so there is nobody to ask for a register."""
    from src.schemas.models import Group
    from src.services.operational_groups import actionable_group_clause

    group = world["group"](name="NUET Special", is_special=True)
    world["enrol"](group)

    actionable = {
        row.id for row in world["db"].query(Group).filter(actionable_group_clause()).all()
    }
    assert group.id not in actionable


def test_an_ordinary_group_is_both_operational_and_actionable(world):
    """The control: the split must not have narrowed the normal case."""
    from src.schemas.models import Group
    from src.services.operational_groups import actionable_group_clause

    group = world["group"]()
    world["enrol"](group)

    actionable = {
        row.id for row in world["db"].query(Group).filter(actionable_group_clause()).all()
    }
    assert group.id in _operational(world)
    assert group.id in actionable


# --- the production case -------------------------------------------------------------------


def test_the_david_indi_group_is_not_operational(world):
    """Production group 79 exactly: switched off, *not* marked finished, nobody enrolled.

    `is_over` being false is the detail that mattered — a filter that only asked "is this
    course finished?" answered "no" and put it on the calendar.
    """
    group = world["group"](name="David Indi SAT - Нурай",
                           is_active=False, is_over=False, is_special=False)
    future = world["lesson"](group)

    assert group.id not in _operational(world)
    assert future.id not in _calendar_event_ids(world), \
        "a future lesson does not make a wound-up group operational"


def test_its_history_is_untouched(world):
    """The predicate decides what to *show*, never what to keep.

    Group 79 carries 142 real lessons. Filtering it off the calendar must leave every one of
    them, and their attendance, exactly where they are.
    """
    group = world["group"](is_active=False)
    world["lesson"](group, days_ahead=-30)
    world["lesson"](group, days_ahead=-20)

    surviving = (
        world["db"].query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(EventGroup.group_id == group.id)
        .count()
    )
    assert surviving == 2, "history is still readable; only the calendar stops showing it"


# --- events attached to several groups -------------------------------------------------------


def test_a_lesson_shared_with_one_live_group_is_operational_and_appears_once(world):
    """"At least one" is the rule — and the lesson is still a single lesson.

    A join-and-filter implementation returns this row once per matching group. This is the
    assertion that catches the duplicate.
    """
    live, wound_up = world["group"](name="live"), world["group"](name="over", is_over=True)
    world["enrol"](live); world["enrol"](wound_up)
    shared = world["lesson"](live, wound_up)

    drawn = _calendar_event_ids(world)
    assert drawn.count(shared.id) == 1


def test_a_lesson_shared_by_two_live_groups_still_appears_once(world):
    a, b = world["group"](name="a"), world["group"](name="b")
    world["enrol"](a); world["enrol"](b)
    shared = world["lesson"](a, b)

    assert _calendar_event_ids(world).count(shared.id) == 1


def test_a_lesson_whose_every_group_stopped_is_not_operational(world):
    dead_a = world["group"](name="a", is_active=False)
    dead_b = world["group"](name="b", is_over=True)
    world["enrol"](dead_a); world["enrol"](dead_b)
    orphan = world["lesson"](dead_a, dead_b)

    assert orphan.id not in _calendar_event_ids(world)


# --- scope -----------------------------------------------------------------------------------


def test_a_scope_narrows_the_result_and_never_widens_it(world):
    mine, theirs = world["group"](name="mine"), world["group"](name="theirs")
    world["enrol"](mine); world["enrol"](theirs)

    assert operational_group_ids(world["db"], within=[mine.id]) == {mine.id}


def test_an_empty_scope_means_nothing_not_everything(world):
    """The classic scoping bug: `if not ids` falling through to an unfiltered query."""
    group = world["group"]()
    world["enrol"](group)

    assert operational_group_ids(world["db"], within=[]) == set()


def test_the_clause_and_the_id_helper_agree(world):
    """Two ways to ask the same question; a calendar uses one and a filter list the other."""
    live = world["group"](name="live"); world["enrol"](live)
    dead = world["group"](name="dead", is_active=False); world["enrol"](dead)

    by_clause = {g.id for g in world["db"].query(Group).filter(operational_group_clause()).all()}
    assert live.id in by_clause and dead.id not in by_clause
    assert by_clause == _operational(world)
