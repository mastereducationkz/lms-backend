"""A make-up lesson keeps the cancelled lesson's length and prefers a slot of that length.

The group meets Mon 18:00 × 60 and Sat 19:00 × 90 (Almaty). Cancelling a Saturday «и добавить
урок в конец курса» used to take the earliest regular slot of any length — a Monday hour
slot — for a 90-minute lesson, which then overran the Monday slot's hour. Course hours stay
as planned either way: the replacement is always as long as the lesson it replaces.
"""
import asyncio
from datetime import date, datetime, time, timedelta

import pytest

from src.lesson_requests import routes as lr_routes
from src.lesson_requests.schemas import CreateLessonRequestSchema, ResolveLessonRequestSchema
from src.lesson_requests.services import ADD_REPLACEMENT, create_lesson_request_record
from src.schemas.models import Event, EventGroup, Group, UserInDB
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
        session.close()
        trans.rollback()
        connection.close()


KZ = timedelta(hours=5)
_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _this_monday() -> date:
    today_local = (datetime.utcnow() + KZ).date()
    return today_local - timedelta(days=today_local.weekday())


def _at(local_day: date, hh: int) -> datetime:
    """``hh:00`` Almaty on ``local_day`` as the naive UTC the events table stores."""
    return datetime.combine(local_day, time(hh, 0)) - KZ


def _user(db, role):
    u = UserInDB(email=f"makeup-{role}{_uniq()}@test.local", name=role.title(), role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _event(db, group, start, minutes, event_type="class", title=None):
    ev = Event(title=title or f"{group.name}: Lesson", event_type=event_type,
               start_datetime=start, end_datetime=start + timedelta(minutes=minutes),
               is_active=True, is_online=True, location="Online",
               teacher_id=group.teacher_id, created_by=group.teacher_id)
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
    return ev


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def course(db):
    """Mon 18:00 × 60 + Sat 19:00 × 90; four lessons ahead, the last one a Saturday."""
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    monday = _this_monday()
    group = Group(
        name=f"Makeup G{_uniq()}", is_active=True, is_over=False, teacher_id=teacher.id,
        program_type="ielts",
        schedule_config={
            "start_date": (monday + timedelta(days=7)).isoformat(), "weeks_count": 4,
            "lessons_count": 4,
            "schedule_items": [
                {"day_of_week": 0, "time_of_day": "18:00", "duration_minutes": 60},
                {"day_of_week": 5, "time_of_day": "19:00", "duration_minutes": 90},
            ],
        },
    )
    db.add(group); db.flush()
    lessons = [
        _event(db, group, _at(monday + timedelta(days=7), 18), 60),    # Mon
        _event(db, group, _at(monday + timedelta(days=12), 19), 90),   # Sat — cancelled below
        _event(db, group, _at(monday + timedelta(days=14), 18), 60),   # Mon
        _event(db, group, _at(monday + timedelta(days=19), 19), 90),   # Sat — the last lesson
    ]
    return {"db": db, "teacher": teacher, "admin": admin, "group": group, "lessons": lessons,
            "monday": monday}


def _cancel_with_replacement(course, target):
    db = course["db"]
    lr = create_lesson_request_record(
        db, course["teacher"],
        CreateLessonRequestSchema(request_type="cancel", event_id=target.id,
                                  group_id=course["group"].id,
                                  original_datetime=target.start_datetime),
    )
    _run(lr_routes.approve_lesson_request(
        request_id=lr.id, data=ResolveLessonRequestSchema(cancel_resolution=ADD_REPLACEMENT),
        db=db, current_user=course["admin"],
    ))
    db.refresh(lr)
    return db.get(Event, lr.replacement_event_id)


def test_a_cancelled_saturday_is_made_up_on_the_next_free_saturday(course):
    replacement = _cancel_with_replacement(course, course["lessons"][1])

    # After the last lesson (Sat +19) the earliest regular slot is Monday +21 — an hour slot.
    # The next Saturday 19:00 is the one that fits a 90-minute lesson.
    assert replacement.start_datetime == _at(course["monday"] + timedelta(days=26), 19)
    assert replacement.end_datetime - replacement.start_datetime == timedelta(minutes=90)


def test_with_no_saturday_free_it_lands_on_monday_and_keeps_ninety_minutes(course):
    db, group, monday = course["db"], course["group"], course["monday"]
    # A non-class event on every Saturday 19:00 of the eight-week search window: occupied,
    # but not a lesson, so the course's last lesson (the anchor) stays Sat +19.
    for week in range(1, 9):
        _event(db, group, _at(monday + timedelta(days=19 + 7 * week), 19), 90,
               event_type="exam", title="Mock exam")

    replacement = _cancel_with_replacement(course, course["lessons"][1])

    assert replacement.start_datetime == _at(monday + timedelta(days=21), 18)
    assert replacement.end_datetime - replacement.start_datetime == timedelta(minutes=90)


def test_slots_carry_their_length_from_the_schedule_and_none_off_events(course):
    from src.lesson_requests.helpers import _regular_slots

    group, lessons = course["group"], course["lessons"]
    assert _regular_slots(group, lessons, lessons[1]) == [(0, time(18, 0), 60), (5, time(19, 0), 90)]

    group.schedule_config = None
    assert _regular_slots(group, lessons, lessons[1]) == [(0, time(18, 0), None), (5, time(19, 0), None)]
