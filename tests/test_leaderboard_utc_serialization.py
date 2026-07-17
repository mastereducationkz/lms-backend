"""
Leaderboard endpoints must serialize event datetimes as UTC ISO strings with
an explicit "Z" suffix. Events are stored naive-UTC; without the suffix the
browser parses them as local time and the leaderboard shows raw UTC (10:00
instead of 15:00 Almaty).

Savepoint-isolated; skips without Postgres.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from src.schemas.models import Group, GroupStudent, UserInDB, Event, EventGroup
from src.gamification.routes.leaderboard import router as leaderboard_router


def _endpoint(path):
    for r in leaderboard_router.routes:
        if getattr(r, "path", None) == path:
            return r.endpoint
    raise RuntimeError(f"route {path} not found")


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


@pytest.fixture
def group_with_event(db):
    admin = _user(db, "utcser_admin@test.local", "admin")
    teacher = _user(db, "utcser_teacher@test.local", "teacher")
    group = Group(name="UTC Ser", program_type="general_english",
                  teacher_id=teacher.id)
    db.add(group)
    db.flush()

    student = _user(db, "utcser_student@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()

    # 15:00 Almaty stored as 10:00 UTC (2026-07-16 is a Thursday).
    start = datetime(2026, 7, 16, 10, 0, 0)
    ev = Event(title="c1", event_type="class", start_datetime=start,
               end_datetime=start + timedelta(hours=1), created_by=teacher.id,
               is_active=True, is_recurring=False)
    db.add(ev)
    db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group.id))
    db.flush()
    return admin, group


def test_weekly_lessons_start_datetime_has_utc_marker(db, group_with_event):
    admin, group = group_with_event
    res = _maybe_run(_endpoint("/curator/weekly-lessons/{group_id}")(
        group.id, week_number=1, current_user=admin, db=db))

    sd = res["lessons"][0]["start_datetime"]
    assert isinstance(sd, str)
    assert sd == "2026-07-16T10:00:00Z"


def test_full_attendance_start_datetime_has_utc_marker(db, group_with_event):
    admin, group = group_with_event
    res = _endpoint("/curator/full-attendance/{group_id}")(
        group.id, current_user=admin, db=db)

    sd = res["lessons"][0]["start_datetime"]
    assert isinstance(sd, str)
    assert sd == "2026-07-16T10:00:00Z"
