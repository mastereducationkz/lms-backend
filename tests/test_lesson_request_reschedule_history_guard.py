"""A lesson that was taught and marked cannot be rescheduled into the future.

A reschedule rewrites ``events.start_datetime`` in place, and attendance rows point at the
event rather than at a date. That is deliberate — moving next Monday's lesson to Friday must
keep its history — but applied to a lesson that already happened, the same mechanism carries
a teacher's marks onto a date the class was never in the room.

Production did it: «June 9 SAT - Ayanat» lessons taught and marked on 27 and 29 July were
rescheduled on 22 August to 1 and 3 September, and twenty students' marks travelled with
them. The cards then showed attendance filled in for lessons that had not happened yet.
Five groups carried the same damage before it was noticed, and it was never a marking bug.

The line is the mark, not the date: a past lesson nobody marked stays reschedulable, because
«мы не провели понедельник, перенесём на пятницу» is a real request and refusing it would
break a working flow to fix a different problem.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.lesson_requests.helpers import apply_reschedule
from src.lesson_requests.schemas import CreateLessonRequestSchema
from src.lesson_requests.services import create_lesson_request_record
from src.schemas.models import (
    Attendance,
    Event,
    EventGroup,
    Group,
    LessonRequest,
    UserInDB,
)
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


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _teacher(db):
    u = UserInDB(
        email=f"resched-t{_uniq()}@test.local", name="T", role="teacher",
        hashed_password=hash_password("x"), is_active=True,
    )
    db.add(u); db.flush(); return u


def _student(db):
    u = UserInDB(
        email=f"resched-s{_uniq()}@test.local", name="S", role="student",
        hashed_password=hash_password("x"), is_active=True,
    )
    db.add(u); db.flush(); return u


def _lesson(db, teacher, *, when, marks=()):
    """A group with one lesson at ``when``, carrying ``marks`` as attendance statuses."""
    group = Group(name=f"G{_uniq()}", is_active=True, is_over=False,
                  teacher_id=teacher.id, program_type="sat")
    db.add(group); db.flush()
    ev = Event(
        title="Lesson", event_type="class",
        start_datetime=when, end_datetime=when + timedelta(hours=1),
        is_active=True, teacher_id=teacher.id, created_by=teacher.id,
    )
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
    for status in marks:
        db.add(Attendance(event_id=ev.id, user_id=_student(db).id, status=status))
    db.flush()
    return group, ev


def _ask_reschedule(db, teacher, group, ev):
    return create_lesson_request_record(
        db, teacher,
        CreateLessonRequestSchema(
            request_type="reschedule",
            event_id=ev.id,
            group_id=group.id,
            original_datetime=ev.start_datetime,
            new_datetime=datetime.utcnow() + timedelta(days=7),
        ),
    )


PAST = datetime.utcnow() - timedelta(days=30)
FUTURE = datetime.utcnow() + timedelta(days=3)


def test_a_taught_lesson_cannot_be_rescheduled(db):
    teacher = _teacher(db)
    group, ev = _lesson(db, teacher, when=PAST, marks=("present", "absent", "late"))

    with pytest.raises(HTTPException) as excinfo:
        _ask_reschedule(db, teacher, group, ev)

    assert excinfo.value.status_code == 400
    # The number is in the message on purpose: «уже проведён» invites an argument,
    # «выставлено отметок — 3» does not.
    assert "3" in excinfo.value.detail
    assert "уже проведён" in excinfo.value.detail


def test_a_past_lesson_nobody_marked_is_still_reschedulable(db):
    """The flow this guard must not break."""
    teacher = _teacher(db)
    group, ev = _lesson(db, teacher, when=PAST)

    request = _ask_reschedule(db, teacher, group, ev)
    assert request.id is not None
    assert request.request_type == "reschedule"


def test_roster_and_cancellation_rows_are_not_marks(db):
    """«registered» is a roster row and «cancelled» records a lesson that did not happen."""
    teacher = _teacher(db)
    group, ev = _lesson(db, teacher, when=PAST, marks=("registered", "cancelled", "removed"))

    request = _ask_reschedule(db, teacher, group, ev)
    assert request.id is not None


def test_legacy_mark_spellings_count_too(db):
    """The reason this classifies through ``attendance_status`` instead of a local tuple.

    Imported rows predate the current writer and spell themselves «missed», «presented»,
    «1»/«0». A hand-written present/late/absent list would wave those lessons through, which
    is the same bug with a narrower blast radius.
    """
    teacher = _teacher(db)
    group, ev = _lesson(db, teacher, when=PAST, marks=("missed",))

    with pytest.raises(HTTPException) as excinfo:
        _ask_reschedule(db, teacher, group, ev)
    assert excinfo.value.status_code == 400


def test_a_future_lesson_is_reschedulable_even_when_it_has_marks(db):
    """The guard is about rewriting history, not about the presence of attendance rows."""
    teacher = _teacher(db)
    group, ev = _lesson(db, teacher, when=FUTURE, marks=("present",))

    request = _ask_reschedule(db, teacher, group, ev)
    assert request.id is not None


def test_approval_refuses_when_the_lesson_was_marked_after_the_request(db):
    """The gap the creation-time check alone cannot close.

    A request sits pending for days. The lesson it names is taught and marked in the
    meantime. Approval is the moment the move actually happens, so it is the moment that has
    to be sure — this is exactly the production sequence, where the request was filed at
    06:29 and approved at 10:41.
    """
    teacher = _teacher(db)
    group, ev = _lesson(db, teacher, when=PAST)
    request = _ask_reschedule(db, teacher, group, ev)

    db.add(Attendance(event_id=ev.id, user_id=_student(db).id, status="present"))
    db.flush()

    with pytest.raises(HTTPException) as excinfo:
        apply_reschedule(db, request, teacher.id)
    assert excinfo.value.status_code == 400

    db.refresh(ev)
    assert ev.start_datetime == PAST, "the lesson must not have moved"


def test_an_approved_reschedule_of_an_unmarked_lesson_still_moves_it(db):
    """The feature still works — the guard is a narrow refusal, not a disabling."""
    teacher = _teacher(db)
    group, ev = _lesson(db, teacher, when=PAST)
    request = _ask_reschedule(db, teacher, group, ev)
    target = request.new_datetime

    apply_reschedule(db, request, teacher.id)

    db.refresh(ev)
    assert ev.start_datetime == target
