"""
The weekly-lessons leaderboard must not count lessons that ended before a
student's effective join date against them. The CRM writes the manager-picked
"joined_from" into GroupStudent.created_at and backfills attendance only from
that date forward, so earlier lessons have no record and used to render as
ABSENT (dragging the % down). Those lessons must now come back with
enrolled=False; lessons on/after the join date stay enrolled=True. A real
attendance record on a pre-join lesson is never hidden.

Savepoint-isolated; skips without Postgres.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from src.schemas.models import Group, GroupStudent, UserInDB, Event, EventGroup
from src.events.models import Attendance
from src.gamification.routes.leaderboard import router as leaderboard_router


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


def _lessons_by_number(res, student_id):
    row = next(s for s in res["students"] if s["student_id"] == student_id)
    return row["lessons"]


def test_pre_join_lessons_flagged_not_enrolled(db):
    admin = _user(db, "enroll_admin@test.local", "admin")
    teacher = _user(db, "enroll_teacher@test.local", "teacher")
    group = Group(name="GE Enrollment", program_type="general_english",
                  teacher_id=teacher.id)
    db.add(group)
    db.flush()

    # Two class lessons in week 1 (2026-06-01 is a Monday).
    e1_start = datetime(2026, 6, 1, 10, 0, 0)   # before join
    e2_start = datetime(2026, 6, 3, 10, 0, 0)   # on join day
    e1 = Event(title="l1", event_type="class", start_datetime=e1_start,
               end_datetime=e1_start + timedelta(hours=1), created_by=teacher.id,
               is_active=True, is_recurring=False)
    e2 = Event(title="l2", event_type="class", start_datetime=e2_start,
               end_datetime=e2_start + timedelta(hours=1), created_by=teacher.id,
               is_active=True, is_recurring=False)
    db.add_all([e1, e2])
    db.flush()
    db.add_all([EventGroup(event_id=e1.id, group_id=group.id),
                EventGroup(event_id=e2.id, group_id=group.id)])

    # Joined effective 2026-06-03 → lesson on 06-01 predates membership.
    late = _user(db, "enroll_late@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=late.id,
                        created_at=datetime(2026, 6, 3, 0, 0, 0)))
    # A founding member (no join date impact) as control.
    founder = _user(db, "enroll_founder@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=founder.id,
                        created_at=datetime(2026, 5, 1, 0, 0, 0)))
    db.flush()

    res = _maybe_run(_weekly_lessons_endpoint()(
        group.id, week_number=1, current_user=admin, db=db))

    late_lessons = _lessons_by_number(res, late.id)
    # Lesson 1 (06-01) predates the 06-03 join → not enrolled.
    assert late_lessons["1"]["enrolled"] is False
    # Lesson 2 (06-03) is on the join day → enrolled.
    assert late_lessons["2"]["enrolled"] is True

    founder_lessons = _lessons_by_number(res, founder.id)
    assert founder_lessons["1"]["enrolled"] is True
    assert founder_lessons["2"]["enrolled"] is True


def test_real_attendance_on_pre_join_lesson_survives(db):
    admin = _user(db, "enroll2_admin@test.local", "admin")
    teacher = _user(db, "enroll2_teacher@test.local", "teacher")
    group = Group(name="GE Enrollment 2", program_type="general_english",
                  teacher_id=teacher.id)
    db.add(group)
    db.flush()

    e1_start = datetime(2026, 6, 1, 10, 0, 0)   # before join, but attended
    e1 = Event(title="trial", event_type="class", start_datetime=e1_start,
               end_datetime=e1_start + timedelta(hours=1), created_by=teacher.id,
               is_active=True, is_recurring=False)
    db.add(e1)
    db.flush()
    db.add(EventGroup(event_id=e1.id, group_id=group.id))

    student = _user(db, "enroll2_student@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=student.id,
                        created_at=datetime(2026, 6, 3, 0, 0, 0)))
    # A real "present" record exists for the pre-join lesson (e.g. trial class).
    db.add(Attendance(event_id=e1.id, user_id=student.id, status="present"))
    db.flush()

    res = _maybe_run(_weekly_lessons_endpoint()(
        group.id, week_number=1, current_user=admin, db=db))

    lessons = _lessons_by_number(res, student.id)
    # Real attendance is never hidden — stays enrolled and shows the mark.
    assert lessons["1"]["enrolled"] is True
    assert lessons["1"]["attendance_status"] == "attended"
