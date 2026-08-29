"""A register cannot be taken for a class that has not met.

This is the second lock on the same door. The first — in `lesson_requests` — stops a lesson
that was taught and marked from being rescheduled into the future, which is how production
came to show attendance filled in for September lessons that had not happened: they were
July lessons whose dates were rewritten, and the marks travelled with the event.

Closing only that hole would leave the state reachable by writing, so marking a lesson that
is genuinely still ahead is refused too.

The tolerance matters as much as the rule. Event datetimes are stored naive while the school
runs on Almaty time, so a strict «starts after now» would refuse a teacher marking a class
that has obviously already begun for them the moment any part of that pipeline is off by
hours. A day of slack cannot be reached by a timezone and cannot hide a lesson weeks ahead
of itself.
"""
from datetime import datetime, timedelta

import pytest

# `src.schemas.models` first, deliberately. `src.services.attendance_service` imports
# `src.events.models` directly, and importing that before the model package has finished
# loading trips a pre-existing circular import. Every other test reaches the models first by
# accident; this one has to do it on purpose.
from src.schemas.models import Event, UserInDB  # isort: skip
from src.services.attendance_service import MARKABLE_GRACE_DAYS, AttendanceService
from src.utils.auth_utils import hash_password


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
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
        session.close(); trans.rollback(); connection.close()


_seq = 0


def _event_at(db, when):
    global _seq
    _seq += 1
    teacher = UserInDB(
        email=f"future-guard-t{_seq}@test.local", name="T", role="teacher",
        hashed_password=hash_password("x"), is_active=True,
    )
    db.add(teacher); db.flush()
    ev = Event(
        title="Lesson", event_type="class",
        start_datetime=when, end_datetime=when + timedelta(hours=1),
        is_active=True, teacher_id=teacher.id, created_by=teacher.id,
    )
    db.add(ev); db.flush()
    return ev


def test_a_lesson_weeks_ahead_cannot_be_marked(db):
    ev = _event_at(db, datetime.utcnow() + timedelta(days=30))
    reason = AttendanceService.event_is_unmarkable_because_future(db, ev.id)
    assert reason is not None
    # The date is in the message: «ещё не проведён» invites an argument about which lesson.
    assert ev.start_datetime.strftime("%d.%m.%Y") in reason


def test_a_lesson_that_has_happened_is_markable(db):
    ev = _event_at(db, datetime.utcnow() - timedelta(days=1))
    assert AttendanceService.event_is_unmarkable_because_future(db, ev.id) is None


def test_a_lesson_starting_shortly_is_markable(db):
    """Marking as the class begins is the normal case, not an edge one."""
    ev = _event_at(db, datetime.utcnow() + timedelta(minutes=30))
    assert AttendanceService.event_is_unmarkable_because_future(db, ev.id) is None


def test_the_grace_window_absorbs_a_timezone_sized_error(db):
    """Almaty is UTC+5; nothing in that range may be refused."""
    ev = _event_at(db, datetime.utcnow() + timedelta(hours=5))
    assert AttendanceService.event_is_unmarkable_because_future(db, ev.id) is None


def test_the_boundary_is_the_grace_window_and_not_now(db):
    just_inside = _event_at(db, datetime.utcnow() + timedelta(days=MARKABLE_GRACE_DAYS) - timedelta(hours=1))
    just_outside = _event_at(db, datetime.utcnow() + timedelta(days=MARKABLE_GRACE_DAYS) + timedelta(hours=1))
    assert AttendanceService.event_is_unmarkable_because_future(db, just_inside.id) is None
    assert AttendanceService.event_is_unmarkable_because_future(db, just_outside.id) is not None


def test_a_missing_event_is_not_this_check_s_problem(db):
    """Absent or undated events fall through — some other layer owns that error."""
    assert AttendanceService.event_is_unmarkable_because_future(db, 99_999_999) is None
