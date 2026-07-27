"""
Tests that the per-event attendance endpoints carry activity score.

Mirrors tests/test_lesson_topic.py: real Postgres session with SAVEPOINT
rollback, endpoint functions invoked directly (no HTTP/JWT).
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.config import SessionLocal  # noqa: F401  (kept for parity)
from src.schemas.models import (
    AttendanceBulkUpdateSchema,
    AttendanceRecord,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    UserInDB,
)
from src.events.routes.events import (
    get_event_participants,
    update_event_attendance,
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
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x", role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


def _class_event(db, group_id, created_by, start=None, event_type="class"):
    start = start or datetime.now(timezone.utc).replace(microsecond=0)
    ev = Event(
        title="G: Lesson 1", event_type=event_type, start_datetime=start,
        end_datetime=start + timedelta(hours=1), created_by=created_by,
        is_active=True, is_recurring=False,
    )
    db.add(ev)
    db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group_id))
    db.flush()
    return ev


def test_event_attendance_persists_and_returns_activity_score(db):
    teacher = _user(db, "att_act_t@test.local", "teacher")
    student = _user(db, "att_act_s@test.local", "student")
    group = Group(name="Act Group", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()
    ev = _class_event(db, group.id, teacher.id)

    update_event_attendance(
        ev.id,
        AttendanceBulkUpdateSchema(
            attendance=[AttendanceRecord(student_id=student.id, status="attended", activity_score=7)]
        ),
        db=db,
        current_user=teacher,
    )

    parts = get_event_participants(ev.id, group_id=group.id, db=db, current_user=teacher)
    row = next(p for p in parts if p.student_id == student.id)
    assert row.attendance_status == "attended"
    assert row.activity_score == 7
