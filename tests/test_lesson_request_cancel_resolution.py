"""Approving a cancel chooses «только отменить» or «отменить и добавить урок в конец курса».

A cancelled lesson used to simply vanish: deactivated, marked «cancelled», gone from
payroll and the loss report as if it never happened. That is still one outcome
(``cancel_only``). The other (``add_replacement``) appends one lesson after the group's last
scheduled one, on the group's regular slot, so the course keeps its planned length.

The fixtures build a group that meets Mon+Thu 19:00 Almaty (14:00 UTC — events are naive
UTC) with six lessons, four in the past and two ahead, and cancel a future one.
"""
import asyncio
from datetime import date, datetime, time, timedelta

import pytest
from fastapi import HTTPException

from src.lesson_requests import routes as lr_routes
from src.lesson_requests.helpers import enrich_requests
from src.lesson_requests.schemas import CreateLessonRequestSchema, ResolveLessonRequestSchema
from src.lesson_requests.services import ADD_REPLACEMENT, CANCEL_ONLY, create_lesson_request_record
from src.schemas.models import (
    Course,
    CourseHeadTeacher,
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


KZ = timedelta(hours=5)
SLOT = time(19, 0)          # the group's slot, Almaty
MON, THU = 0, 3
SCHEDULE_ITEMS = [
    {"day_of_week": MON, "time_of_day": "19:00"},
    {"day_of_week": THU, "time_of_day": "19:00"},
]
#: Day offsets from this week's Monday: four lessons behind us, two ahead.
DEFAULT_OFFSETS = (-14, -11, -7, -4, 7, 10)

_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _this_monday() -> date:
    today_local = (datetime.utcnow() + KZ).date()
    return today_local - timedelta(days=today_local.weekday())


def _slot_utc(local_day: date) -> datetime:
    """19:00 Almaty on ``local_day`` as the naive UTC the events table stores."""
    return datetime.combine(local_day, SLOT) - KZ


def _user(db, role, name=None):
    u = UserInDB(
        email=f"cancel-{role}{_uniq()}@test.local", name=name or role.title(), role=role,
        hashed_password=hash_password("x"), is_active=True,
    )
    db.add(u); db.flush(); return u


def _lesson(db, group, *, start, minutes=60, title=None, event_type="class", teacher_id=None):
    ev = Event(
        title=title if title is not None else f"{group.name}: Lesson",
        event_type=event_type,
        start_datetime=start, end_datetime=start + timedelta(minutes=minutes),
        is_active=True, is_online=True, location="Online",
        teacher_id=teacher_id if teacher_id is not None else group.teacher_id,
        created_by=group.teacher_id,
    )
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
    return ev


def _course(db, teacher, *, offsets=DEFAULT_OFFSETS, with_config=True, lessons_count=6, minutes=90):
    """A group with one 90-minute lesson per offset (days from this Monday), titled 1..N."""
    monday = _this_monday()
    config = None
    if with_config:
        config = {
            "start_date": (monday + timedelta(days=min(offsets))).isoformat(),
            "weeks_count": 5,
            "lessons_count": lessons_count,
            "schedule_items": SCHEDULE_ITEMS,
        }
    group = Group(
        name=f"Cancel G{_uniq()}", is_active=True, is_over=False,
        teacher_id=teacher.id, program_type="sat", schedule_config=config,
    )
    db.add(group); db.flush()
    events = [
        _lesson(db, group, start=_slot_utc(monday + timedelta(days=off)), minutes=minutes,
                title=f"{group.name}: Lesson {i}")
        for i, off in enumerate(offsets, start=1)
    ]
    return group, events


def _cancel_request(db, teacher, group, event, cancel_resolution=None) -> LessonRequest:
    return create_lesson_request_record(
        db, teacher,
        CreateLessonRequestSchema(
            request_type="cancel", event_id=event.id, group_id=group.id,
            original_datetime=event.start_datetime, cancel_resolution=cancel_resolution,
        ),
    )


def _run(coro):
    """Drive an async handler from a sync test, on a loop of our own.

    Neither ambient-loop spelling survives the full suite. ``asyncio.get_event_loop()``
    raises "no current event loop" once any earlier module has called ``asyncio.run()``
    (which unsets the thread's loop on exit) — and many do. ``asyncio.run()`` here would
    then break the sibling modules that still read the ambient loop. A private loop, never
    installed on the thread, leaves that global exactly as it was found.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _approve(db, approver, lr, data=None, **kwargs):
    data = data or ResolveLessonRequestSchema(**kwargs)
    return _run(
        lr_routes.approve_lesson_request(request_id=lr.id, data=data, db=db, current_user=approver)
    )


def _active_class(db, group_id):
    return sorted(
        db.query(Event).join(EventGroup, EventGroup.event_id == Event.id).filter(
            EventGroup.group_id == group_id, Event.event_type == "class", Event.is_active == True,
        ).all(),
        key=lambda e: (e.start_datetime, e.id),
    )


def _titles(db, group):
    return [e.title for e in _active_class(db, group.id)]


def _numbered(group, n):
    return [f"{group.name}: Lesson {i}" for i in range(1, n + 1)]


# ── 1. cancel_only is the default, and today's behaviour ──────────────────────────────


def test_approving_without_a_choice_is_cancel_only(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    target = events[4]                                     # next Monday
    lr = _cancel_request(db, teacher, group, target)

    out = _approve(db, admin, lr)                          # cancel_resolution omitted

    db.refresh(lr); db.refresh(target); db.refresh(group)
    assert out.status == "approved"
    assert lr.cancel_resolution == CANCEL_ONLY == out.cancel_resolution
    assert target.is_active is False
    assert lr.replacement_event_id is None and out.replacement_event_id is None
    # One lesson fewer in the plan, or the course could never become «Завершена».
    assert group.schedule_config["lessons_count"] == 5
    assert len(_active_class(db, group.id)) == 5
    assert _titles(db, group) == _numbered(group, 5), "renumbered without a gap"
    assert group.is_over is False


def test_cancel_only_on_a_finished_course_marks_it_over(db):
    """The reason lessons_count shrinks: a 6-lesson course with 5 taught and the 6th cancelled
    is finished, and the CRM's «Завершил» queue is waiting for it."""
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher, offsets=(-28, -25, -21, -18, -14, -11))
    lr = _cancel_request(db, teacher, group, events[5])

    _approve(db, admin, lr, cancel_resolution=CANCEL_ONLY)

    db.refresh(group)
    assert group.schedule_config["lessons_count"] == 5
    assert group.is_over is True


# ── 2. add_replacement appends one lesson after the last scheduled one ───────────────


def test_add_replacement_appends_a_lesson_on_the_next_regular_slot(db):
    teacher, admin, substitute = _user(db, "teacher"), _user(db, "admin"), _user(db, "teacher")
    group, events = _course(db, teacher)                   # last lesson: Thursday next week
    target = events[4]
    target.teacher_id = substitute.id                      # a substitute pinned to this one
    db.flush()
    lr = _cancel_request(db, teacher, group, target)

    out = _approve(db, admin, lr, cancel_resolution=ADD_REPLACEMENT)

    db.refresh(lr); db.refresh(group)
    assert lr.cancel_resolution == ADD_REPLACEMENT == out.cancel_resolution
    assert lr.replacement_event_id is not None
    replacement = db.get(Event, lr.replacement_event_id)
    assert replacement.is_active is True and replacement.event_type == "class"
    assert db.query(EventGroup).filter_by(event_id=replacement.id, group_id=group.id).count() == 1
    # The Monday after the last lesson (Thursday +10), 19:00 Almaty.
    assert replacement.start_datetime == _slot_utc(_this_monday() + timedelta(days=14))
    assert replacement.end_datetime - replacement.start_datetime == timedelta(minutes=90)
    assert replacement.teacher_id == group.teacher_id != substitute.id
    assert replacement.created_by == admin.id
    assert "Replacement for the cancelled lesson" in (replacement.description or "")
    # The plan is intact: one lesson moved to the end.
    assert group.schedule_config["lessons_count"] == 6
    assert _titles(db, group) == _numbered(group, 6)
    assert replacement.title == f"{group.name}: Lesson 6"
    assert group.is_over is False
    assert out.replacement_event_id == replacement.id
    assert out.replacement_lesson_title == replacement.title
    assert out.replacement_datetime == replacement.start_datetime


def test_renumbering_leaves_custom_titles_alone(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    events[0].title = "Intro session"                      # hand-written, not ": Lesson N"
    db.flush()
    lr = _cancel_request(db, teacher, group, events[4])

    _approve(db, admin, lr, cancel_resolution=ADD_REPLACEMENT)

    titles = _titles(db, group)
    assert titles[0] == "Intro session"
    # Position counts, so the auto-titled ones are 2..6 — the same rule as the CRM's retitle.
    assert titles[1:] == [f"{group.name}: Lesson {i}" for i in range(2, 7)]


# ── 3. slots fall back to the existing lessons when there is no schedule_config ──────


def test_slots_are_read_off_existing_lessons_without_schedule_config(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher, with_config=False)
    assert group.schedule_config is None
    lr = _cancel_request(db, teacher, group, events[4])

    _approve(db, admin, lr, cancel_resolution=ADD_REPLACEMENT)

    db.refresh(lr)
    replacement = db.get(Event, lr.replacement_event_id)
    assert replacement.start_datetime == _slot_utc(_this_monday() + timedelta(days=14))
    assert group.schedule_config is None, "nothing to decrement, nothing invented"


# ── 4. an occupied slot is skipped ────────────────────────────────────────────────────


def test_an_occupied_slot_is_skipped_for_the_next_one(db):
    """Only a non-class event can sit on the candidate instant: an active *class* event there
    would itself be the anchor, and the candidate is strictly after the anchor."""
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    candidate = _slot_utc(_this_monday() + timedelta(days=14))
    _lesson(db, group, start=candidate, event_type="exam", title="Mock exam")
    lr = _cancel_request(db, teacher, group, events[4])

    _approve(db, admin, lr, cancel_resolution=ADD_REPLACEMENT)

    db.refresh(lr)
    replacement = db.get(Event, lr.replacement_event_id)
    assert replacement.start_datetime == _slot_utc(_this_monday() + timedelta(days=17)), "Thursday"


# ── 5. a course entirely in the past gets its replacement in the future ──────────────


def test_replacement_for_a_finished_course_lands_after_now(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher, offsets=(-28, -25, -21, -18, -14, -11))
    lr = _cancel_request(db, teacher, group, events[5])    # a past lesson — allowed

    _approve(db, admin, lr, cancel_resolution=ADD_REPLACEMENT)

    db.refresh(lr); db.refresh(group)
    replacement = db.get(Event, lr.replacement_event_id)
    now = datetime.utcnow()
    assert replacement.start_datetime > now
    today_local = (now + KZ).date()
    expected = next(
        _slot_utc(today_local + timedelta(days=d))
        for d in range(0, 8)
        if (today_local + timedelta(days=d)).weekday() in (MON, THU)
        and _slot_utc(today_local + timedelta(days=d)) > now
    )
    assert replacement.start_datetime == expected
    assert group.schedule_config["lessons_count"] == 6
    assert group.is_over is False, "a future lesson keeps the course open"


# ── 6. validation ─────────────────────────────────────────────────────────────────────


def test_an_invalid_resolution_is_refused_and_nothing_changes(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    target = events[4]
    lr = _cancel_request(db, teacher, group, target)
    # Past the schema on purpose: the row's own proposal can be garbage too.
    bogus = ResolveLessonRequestSchema.model_construct(cancel_resolution="bogus")

    with pytest.raises(HTTPException) as excinfo:
        _approve(db, admin, lr, data=bogus)

    assert excinfo.value.status_code == 400
    db.refresh(lr); db.refresh(target)
    assert lr.status == "pending"
    assert target.is_active is True
    assert len(_active_class(db, group.id)) == 6


def test_a_non_cancel_request_ignores_the_field(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    target = events[4]
    new_dt = target.start_datetime + timedelta(days=1)
    lr = create_lesson_request_record(
        db, teacher,
        CreateLessonRequestSchema(
            request_type="reschedule", event_id=target.id, group_id=group.id,
            original_datetime=target.start_datetime, new_datetime=new_dt,
            cancel_resolution=ADD_REPLACEMENT,             # meaningless here
        ),
    )
    assert lr.cancel_resolution is None

    out = _approve(db, admin, lr, cancel_resolution=ADD_REPLACEMENT)

    db.refresh(lr); db.refresh(target)
    assert out.status == "approved"
    assert lr.cancel_resolution is None and lr.replacement_event_id is None
    assert target.start_datetime == new_dt
    assert len(_active_class(db, group.id)) == 6


def test_the_teachers_proposal_applies_when_the_approver_stays_silent(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    lr = _cancel_request(db, teacher, group, events[4], cancel_resolution=ADD_REPLACEMENT)
    assert lr.cancel_resolution == ADD_REPLACEMENT, "stored as the proposal"

    _approve(db, admin, lr)                                # no choice from the approver

    db.refresh(lr)
    assert lr.cancel_resolution == ADD_REPLACEMENT
    assert lr.replacement_event_id is not None


# ── 7. serialisation ──────────────────────────────────────────────────────────────────


def test_enrich_requests_exposes_the_resolution_and_the_replacement(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    pending = _cancel_request(db, teacher, group, events[4], cancel_resolution=CANCEL_ONLY)
    approved = _cancel_request(db, teacher, group, events[5])
    _approve(db, admin, approved, cancel_resolution=ADD_REPLACEMENT)
    db.refresh(approved)
    replacement = db.get(Event, approved.replacement_event_id)

    by_id = {s.id: s for s in enrich_requests([pending, approved], db)}

    assert by_id[pending.id].cancel_resolution == CANCEL_ONLY
    assert by_id[pending.id].replacement_event_id is None
    assert by_id[pending.id].replacement_lesson_title is None
    assert by_id[pending.id].replacement_datetime is None

    assert by_id[approved.id].cancel_resolution == ADD_REPLACEMENT
    assert by_id[approved.id].replacement_event_id == replacement.id
    assert by_id[approved.id].replacement_lesson_title == replacement.title
    assert by_id[approved.id].replacement_datetime == replacement.start_datetime
    # Serialised like every other datetime: naive UTC with a trailing Z.
    payload = by_id[approved.id].model_dump_json()
    assert replacement.start_datetime.isoformat() + "Z" in payload


# ── 8. a teacher who heads the subject self-approves ──────────────────────────────────


def _head_of_sat(db, teacher):
    course = Course(title="SAT", course_type="sat", is_active=True)
    db.add(course); db.flush()
    db.add(CourseHeadTeacher(course_id=course.id, head_teacher_id=teacher.id)); db.flush()


def _self_file_cancel(db, teacher, group, event, cancel_resolution=None):
    return _run(
        lr_routes.create_lesson_request(
            CreateLessonRequestSchema(
                request_type="cancel", event_id=event.id, group_id=group.id,
                original_datetime=event.start_datetime, cancel_resolution=cancel_resolution,
            ),
            db=db, current_user=teacher,
        )
    )


def test_self_approved_cancel_without_a_choice_is_cancel_only(db):
    teacher = _user(db, "teacher")
    _head_of_sat(db, teacher)
    group, events = _course(db, teacher)

    out = _self_file_cancel(db, teacher, group, events[4])

    assert out.status == "approved"
    assert out.cancel_resolution == CANCEL_ONLY
    assert out.replacement_event_id is None
    db.refresh(group); db.refresh(events[4])
    assert events[4].is_active is False
    assert group.schedule_config["lessons_count"] == 5
    assert _titles(db, group) == _numbered(group, 5)


def test_self_approved_cancel_honours_the_teachers_own_choice(db):
    teacher = _user(db, "teacher")
    _head_of_sat(db, teacher)
    group, events = _course(db, teacher)

    out = _self_file_cancel(db, teacher, group, events[4], cancel_resolution=ADD_REPLACEMENT)

    assert out.status == "approved"
    assert out.cancel_resolution == ADD_REPLACEMENT
    assert out.replacement_event_id is not None
    assert out.replacement_datetime == _slot_utc(_this_monday() + timedelta(days=14))
    db.refresh(group)
    assert group.schedule_config["lessons_count"] == 6
    assert _titles(db, group) == _numbered(group, 6)
