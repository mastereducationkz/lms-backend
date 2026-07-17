"""
The weekly-lessons leaderboard endpoint must include each assignment's
max_score in lessons[].homework so the frontend can normalize HW scores
and show a denominator for unsubmitted homework.

Savepoint-isolated; skips without Postgres.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from src.schemas.models import Group, GroupStudent, UserInDB, Event, EventGroup
from src.assignments.models import Assignment
from src.gamification.routes.leaderboard import router as leaderboard_router


# Two functions in the module share the name get_weekly_lessons_with_hw_status;
# grab the live /curator/weekly-lessons/ endpoint from the router.
def _weekly_lessons_endpoint():
    for r in leaderboard_router.routes:
        if getattr(r, "path", None) == "/curator/weekly-lessons/{group_id}":
            return r.endpoint
    raise RuntimeError("weekly-lessons route not found")


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
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        trans.rollback()
        connection.close()


def _user(db, email, role):
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x",
                 role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


def _maybe_run(result):
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


def test_lessons_meta_includes_hw_max_score(db):
    admin = _user(db, "hwmeta_admin@test.local", "admin")
    teacher = _user(db, "hwmeta_teacher@test.local", "teacher")
    group = Group(name="GE HW Meta", program_type="general_english",
                  teacher_id=teacher.id)
    db.add(group)
    db.flush()

    student = _user(db, "hwmeta_student@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()

    # One class event in week 1 (2026-06-01 is a Monday).
    start = datetime(2026, 6, 1, 10, 0, 0)
    ev = Event(title="c1", event_type="class", start_datetime=start,
               end_datetime=start + timedelta(hours=1), created_by=teacher.id,
               is_active=True, is_recurring=False)
    db.add(ev)
    db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group.id))

    # Assignment tied to lesson 1 with a non-default max score.
    a = Assignment(title="HW1", group_id=group.id, lesson_number=1,
                   assignment_type="test", content="{}",
                   max_score=30, is_active=True)
    db.add(a)
    db.flush()

    res = _maybe_run(_weekly_lessons_endpoint()(
        group.id, week_number=1, current_user=admin, db=db))

    hw = res["lessons"][0]["homework"]
    assert hw is not None
    assert hw["max_score"] == 30
