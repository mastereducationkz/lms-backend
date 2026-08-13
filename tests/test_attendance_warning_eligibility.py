"""A warning is a request for action, so it must be actionable.

Gulzada saw «Attendance Required» for 11 unmarked lessons in a group that had been switched
off and had no active students left. There was nothing she could do and nothing anybody
wanted her to do — the lessons were history nobody had closed the books on. The query behind
the warning asked four things about the *lesson* and nothing at all about the *group*.

Each test below is one of those unasked questions. The last two are the control: a genuinely
live group with a real gap must still warn, or the fix has simply turned the feature off.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.schemas.models import (
    Attendance, Event, EventGroup, Group, GroupStudent, LessonRequest, UserInDB,
)
from src.services.attendance_warnings import warning_eligible_events, warning_summary


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

    def group(**flags):
        g = Group(name="G", teacher_id=teacher.id,
                  **{"is_active": True, "is_over": False, "is_special": False, **flags})
        db.add(g); db.flush()
        return g

    def enrol(group, *, active=True):
        student = _user(db, "student", active=active)
        db.add(GroupStudent(group_id=group.id, student_id=student.id,
                            created_at=datetime.utcnow() - timedelta(days=90)))
        db.flush()
        return student

    def lesson(group, *, days_ago=2, teacher_id=None, cancelled=False, marked=False):
        start = datetime.utcnow() - timedelta(days=days_ago)
        ev = Event(title="Урок", event_type="class", start_datetime=start,
                   end_datetime=start + timedelta(hours=1), is_active=not cancelled,
                   teacher_id=teacher_id if teacher_id is not None else teacher.id,
                   created_by=teacher.id)
        db.add(ev); db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
        if marked:
            member = db.query(GroupStudent).filter_by(group_id=group.id).first()
            db.add(Attendance(event_id=ev.id, user_id=member.student_id, status="present"))
            db.flush()
        return ev

    return {"db": db, "teacher": teacher, "group": group, "enrol": enrol, "lesson": lesson}


def _warned(world, teacher=None):
    tid = (teacher or world["teacher"]).id
    return {e.id for e, _g in warning_eligible_events(world["db"], teacher_id=tid).all()}


# --- the group questions that were never asked -------------------------------------------


def test_an_inactive_group_with_old_unmarked_lessons_warns_nobody(world):
    """The reported case: switched off, 23 past lessons, still nagging."""
    group = world["group"](is_active=False)
    world["enrol"](group)
    lesson = world["lesson"](group)

    assert lesson.id not in _warned(world)


def test_a_completed_group_warns_nobody(world):
    group = world["group"](is_over=True)
    world["enrol"](group)
    lesson = world["lesson"](group)

    assert lesson.id not in _warned(world)


def test_a_special_or_test_group_warns_nobody(world):
    group = world["group"](is_special=True)
    world["enrol"](group)
    lesson = world["lesson"](group)

    assert lesson.id not in _warned(world)


def test_a_group_with_no_active_students_warns_nobody(world):
    """A register with nobody to call is not a task.

    The membership row still exists — the *person* was deactivated — which is exactly what
    made a naive COUNT over group_students think the group was populated.
    """
    group = world["group"]()
    world["enrol"](group, active=False)
    lesson = world["lesson"](group)

    assert lesson.id not in _warned(world)


def test_a_group_with_no_members_at_all_warns_nobody(world):
    group = world["group"]()
    lesson = world["lesson"](group)

    assert lesson.id not in _warned(world)


# --- the lesson questions -----------------------------------------------------------------


def test_a_cancelled_lesson_warns_nobody(world):
    group = world["group"]()
    world["enrol"](group)
    lesson = world["lesson"](group, cancelled=True)

    assert lesson.id not in _warned(world)


def test_a_future_lesson_warns_nobody(world):
    group = world["group"]()
    world["enrol"](group)
    lesson = world["lesson"](group, days_ago=-3)

    assert lesson.id not in _warned(world)


def test_an_already_marked_lesson_warns_nobody(world):
    group = world["group"]()
    world["enrol"](group)
    lesson = world["lesson"](group, marked=True)

    assert lesson.id not in _warned(world)


def test_a_lesson_before_the_launch_cutoff_warns_nobody(world):
    group = world["group"]()
    world["enrol"](group)
    lesson = world["lesson"](group, days_ago=3000)

    assert lesson.id not in _warned(world)


# --- who gets asked ------------------------------------------------------------------------


def test_a_replaced_lesson_warns_only_the_effective_teacher(world):
    """Attendance duty follows whoever is actually teaching it."""
    db = world["db"]
    substitute = _user(db, "teacher")
    group = world["group"]()
    world["enrol"](group)
    covered = world["lesson"](group, teacher_id=substitute.id)
    db.add(LessonRequest(
        request_type="substitution", status="approved",
        requester_id=world["teacher"].id, event_id=covered.id, group_id=group.id,
        original_datetime=datetime.utcnow(), substitute_teacher_id=substitute.id,
    ))
    db.flush()

    assert covered.id in _warned(world, teacher=substitute)
    assert covered.id not in _warned(world), "the group's owner is not asked"


def test_a_lesson_with_no_recorded_teacher_falls_to_the_group_owner(world):
    """Legacy rows predating per-lesson teachers must not go unowned."""
    group = world["group"]()
    world["enrol"](group)
    lesson = world["lesson"](group)
    lesson.teacher_id = None
    world["db"].flush()

    assert lesson.id in _warned(world)


# --- the control: the feature still works --------------------------------------------------


def test_a_live_group_with_a_real_gap_still_warns(world):
    """Without this the fix is indistinguishable from switching the feature off."""
    group = world["group"]()
    world["enrol"](group)
    lesson = world["lesson"](group)

    assert lesson.id in _warned(world)


def test_the_summary_and_the_detail_cannot_disagree(world):
    """They used to be two queries that matched by coincidence."""
    live = world["group"]()
    world["enrol"](live)
    world["lesson"](live, days_ago=5)
    world["lesson"](live, days_ago=2)
    dead = world["group"](is_active=False)
    world["enrol"](dead)
    world["lesson"](dead)

    summary = warning_summary(world["db"], teacher_id=world["teacher"].id)
    detail = _warned(world)

    assert summary["lessons"] == len(detail) == 2
    assert summary["groups"] == 1
    assert summary["lessons"] >= 0 and summary["groups"] >= 0, "never negative"
    assert summary["oldest_missing_date"] is not None


def test_an_empty_scope_returns_nothing_rather_than_everything(world):
    """`group_ids=[]` means "no groups", not "no filter" — the classic empty-IN mistake."""
    group = world["group"]()
    world["enrol"](group)
    world["lesson"](group)

    rows = warning_eligible_events(world["db"], group_ids=[]).all()

    assert rows == []
