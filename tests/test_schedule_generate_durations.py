"""«Сгенерировать расписание» keeps each day's lesson length and counts the course like the CRM.

The generator used to drop ``duration_minutes`` and write every lesson as start + 60, so any
LMS save turned a group's 90-minute Saturdays back into hours. It also counted the course by
replaying the new pattern from ``start_date``, so a pattern changed mid-course scheduled the
wrong number of lessons, and its positional pairing put an approved cancellation back.

Dates are relative to this week's Monday (Almaty) and only whole weeks away from it are used
for past or future lessons, so every case holds whichever weekday the suite runs on.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from src.schemas.models import Event, EventGroup, Group, LessonRequest, UserInDB

UTC = timezone.utc
KZ = timezone(timedelta(hours=5))
MON, TUE, THU, SAT = 0, 1, 3, 5


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


_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _this_monday() -> date:
    today = datetime.now(KZ).date()
    return today - timedelta(days=today.weekday())


def _utc_naive(day: date, hh: int, mm: int = 0) -> datetime:
    """``hh:mm`` Almaty on ``day`` as the naive UTC the events table stores."""
    return datetime.combine(day, time(hh, mm), KZ).astimezone(UTC).replace(tzinfo=None)


@pytest.fixture
def world(db):
    admin = UserInDB(email=f"gen-admin-{_uniq()}-{datetime.utcnow().timestamp()}@test.local",
                     name="Admin", role="admin", hashed_password="x", is_active=True)
    teacher = UserInDB(email=f"gen-teacher-{_uniq()}-{datetime.utcnow().timestamp()}@test.local",
                       name="Teacher", role="teacher", hashed_password="x", is_active=True)
    db.add_all([admin, teacher]); db.flush()
    group = Group(name=f"Gen G{_uniq()}", teacher_id=teacher.id, is_active=True)
    db.add(group); db.flush()
    return {"db": db, "admin": admin, "teacher": teacher, "group": group, "monday": _this_monday()}


def _generate(world, *, start_date, items, lessons_count=None, weeks_count=12):
    from src.gamification.routes.leaderboard import ScheduleGenerationSchema, generate_schedule

    data = ScheduleGenerationSchema(
        group_id=world["group"].id, start_date=start_date, schedule_items=items,
        lessons_count=lessons_count, weeks_count=weeks_count,
    )
    out = generate_schedule(data=data, current_user=world["admin"], db=world["db"])
    world["db"].refresh(world["group"])
    return out


def _lesson(world, start, minutes=60, active=True):
    db, group = world["db"], world["group"]
    ev = Event(title=f"{group.name}: Lesson", event_type="class", start_datetime=start,
               end_datetime=start + timedelta(minutes=minutes), is_active=active,
               created_by=group.teacher_id, teacher_id=group.teacher_id)
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
    return ev


def _active(world, *, future_only=False):
    rows = (
        world["db"].query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(EventGroup.group_id == world["group"].id, Event.event_type == "class",
                Event.is_active == True)  # noqa: E712
        .all()
    )
    now = datetime.utcnow()
    return sorted((e for e in rows if not future_only or e.start_datetime >= now),
                  key=lambda e: (e.start_datetime, e.id))


def _minutes(ev):
    return int((ev.end_datetime - ev.start_datetime).total_seconds() // 60)


def _weekday(ev):
    return ev.start_datetime.replace(tzinfo=UTC).astimezone(KZ).weekday()


MIXED = [
    {"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 60},
    {"day_of_week": SAT, "time_of_day": "19:00", "duration_minutes": 90},
]


# ── (a) durations are stored and applied ─────────────────────────────────────────────


def test_generate_stores_each_days_length_and_writes_lessons_that_long(world):
    start = world["monday"] + timedelta(days=14)

    out = _generate(world, start_date=start, items=MIXED, lessons_count=4)

    lessons = _active(world)
    assert [(e.start_datetime, _minutes(e)) for e in lessons] == [
        (_utc_naive(start, 18), 60),
        (_utc_naive(start + timedelta(days=5), 19), 90),
        (_utc_naive(start + timedelta(days=7), 18), 60),
        (_utc_naive(start + timedelta(days=12), 19), 90),
    ]
    cfg = world["group"].schedule_config
    assert [(i["day_of_week"], i["time_of_day"], i["duration_minutes"]) for i in cfg["schedule_items"]] == [
        (MON, "18:00", 60), (SAT, "19:00", 90),
    ]
    assert cfg["lessons_count"] == 4 and cfg["start_date"] == start.isoformat()
    assert out["message"].startswith("Schedule generated successfully.")


def test_the_stored_schedule_reports_each_days_length(world):
    from src.gamification.routes.leaderboard import GroupScheduleResponse, get_group_schedule

    _generate(world, start_date=world["monday"] + timedelta(days=14), items=MIXED, lessons_count=4)
    raw = get_group_schedule(group_id=world["group"].id, current_user=world["admin"], db=world["db"])

    items = GroupScheduleResponse.model_validate(raw).schedule_items
    assert [(i.day_of_week, i.duration_minutes) for i in items] == [(MON, 60), (SAT, 90)]


def test_a_stored_item_without_a_length_is_reported_as_an_hour(world):
    from src.gamification.routes.leaderboard import GroupScheduleResponse, get_group_schedule

    world["group"].schedule_config = {
        "start_date": world["monday"].isoformat(), "weeks_count": 4, "lessons_count": 8,
        "schedule_items": [{"day_of_week": MON, "time_of_day": "18:00"},
                           {"day_of_week": THU, "time_of_day": "18:00", "duration_minutes": None}],
    }
    world["db"].flush()

    raw = get_group_schedule(group_id=world["group"].id, current_user=world["admin"], db=world["db"])

    items = GroupScheduleResponse.model_validate(raw).schedule_items
    assert [i.duration_minutes for i in items] == [60, 60]


# ── (b) a request without a length inherits the stored one ──────────────────────────


def test_a_request_without_a_length_keeps_the_days_stored_length(world):
    """An old cached client sends no ``duration_minutes``; it must not flatten Saturday."""
    start = world["monday"] + timedelta(days=14)
    _generate(world, start_date=start, items=MIXED, lessons_count=4)

    _generate(world, start_date=start, lessons_count=4, items=[
        {"day_of_week": MON, "time_of_day": "18:00"},
        {"day_of_week": SAT, "time_of_day": "19:00"},
    ])

    assert [_minutes(e) for e in _active(world)] == [60, 90, 60, 90]
    assert [i["duration_minutes"] for i in world["group"].schedule_config["schedule_items"]] == [60, 90]


def test_a_moved_time_inherits_its_days_length_and_a_new_day_gets_an_hour(world):
    start = world["monday"] + timedelta(days=14)
    _generate(world, start_date=start, items=MIXED, lessons_count=4)

    _generate(world, start_date=start, lessons_count=4, items=[
        {"day_of_week": SAT, "time_of_day": "20:00"},   # same day, new time
        {"day_of_week": TUE, "time_of_day": "18:00"},   # a day the group never had
    ])

    by_day = {(i["day_of_week"], i["duration_minutes"]) for i in world["group"].schedule_config["schedule_items"]}
    assert by_day == {(SAT, 90), (TUE, 60)}
    assert {(_weekday(e), _minutes(e)) for e in _active(world)} == {(SAT, 90), (TUE, 60)}


def test_an_explicit_length_wins_over_the_stored_one(world):
    start = world["monday"] + timedelta(days=14)
    _generate(world, start_date=start, items=MIXED, lessons_count=4)

    _generate(world, start_date=start, lessons_count=4, items=[
        {"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 60},
        {"day_of_week": SAT, "time_of_day": "19:00", "duration_minutes": 120},
    ])

    assert [_minutes(e) for e in _active(world)] == [60, 120, 60, 120]


def test_a_length_out_of_range_is_refused():
    from pydantic import ValidationError

    from src.gamification.routes.leaderboard import ScheduleItem

    with pytest.raises(ValidationError):
        ScheduleItem(day_of_week=MON, time_of_day="18:00", duration_minutes=10)
    with pytest.raises(ValidationError):
        ScheduleItem(day_of_week=MON, time_of_day="18:00", duration_minutes=301)


# ── (c) a mid-course change counts the course from lessons taught ────────────────────


def test_a_mid_course_pattern_change_schedules_what_the_course_still_needs(world):
    monday = world["monday"]
    world["group"].schedule_config = {
        "start_date": (monday - timedelta(days=14)).isoformat(), "weeks_count": 7, "lessons_count": 9,
        "schedule_items": [{"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 60},
                           {"day_of_week": THU, "time_of_day": "18:00", "duration_minutes": 60}],
    }
    for offset in (-14, -11, -7):                                  # three lessons taught
        _lesson(world, _utc_naive(monday + timedelta(days=offset), 18))
    for offset in (7, 10, 14, 17, 21, 24):                         # six still to come
        _lesson(world, _utc_naive(monday + timedelta(days=offset), 18))
    world["db"].flush()

    _generate(world, start_date=monday - timedelta(days=14), lessons_count=9, items=[
        {"day_of_week": TUE, "time_of_day": "19:00", "duration_minutes": 60},
        {"day_of_week": SAT, "time_of_day": "19:00", "duration_minutes": 90},
    ])

    future = _active(world, future_only=True)
    assert len(future) == 9 - 3
    assert {_weekday(e) for e in future} == {TUE, SAT}
    assert all(_minutes(e) == (90 if _weekday(e) == SAT else 60) for e in future)
    assert len(_active(world)) == 9


# ── (d) an approved cancellation stays cancelled ────────────────────────────────────


def test_an_approved_cancellation_is_not_resurrected(world):
    db = world["db"]
    start = world["monday"] + timedelta(days=14)
    _generate(world, start_date=start, items=MIXED, lessons_count=4)
    cancelled = _active(world)[1]                                  # the first Saturday
    cancelled_at = cancelled.start_datetime
    cancelled.is_active = False
    db.add(LessonRequest(request_type="cancel", status="approved", event_id=cancelled.id,
                         group_id=world["group"].id, requester_id=world["teacher"].id,
                         original_datetime=cancelled_at))
    # «только отменить» leaves the plan one lesson shorter.
    world["group"].schedule_config = {**world["group"].schedule_config, "lessons_count": 3}
    db.flush()

    _generate(world, start_date=start, items=MIXED, lessons_count=3)

    lessons = _active(world)
    assert cancelled_at not in {e.start_datetime for e in lessons}
    assert db.get(Event, cancelled.id).is_active is False
    assert len(lessons) == 3


def test_excluded_slots_survive_a_save_that_keeps_the_days_and_reset_when_they_change(world):
    start = world["monday"] + timedelta(days=14)
    _generate(world, start_date=start, items=MIXED, lessons_count=4)
    key = f"{(start + timedelta(days=5)).isoformat()}_19:00"
    world["group"].schedule_config = {**world["group"].schedule_config, "excluded_slot_keys": [key]}
    world["db"].flush()

    _generate(world, start_date=start, items=MIXED, lessons_count=4)
    assert world["group"].schedule_config.get("excluded_slot_keys") == [key]
    assert _utc_naive(start + timedelta(days=5), 19) not in {e.start_datetime for e in _active(world)}

    _generate(world, start_date=start, lessons_count=4, items=[MIXED[0]])
    assert not world["group"].schedule_config.get("excluded_slot_keys")


# ── (e) the save recomputes «Завершена» in the same request ─────────────────────────


@pytest.mark.parametrize("was_over, lessons_count, expected", [
    (False, 3, True),    # the count is now met by three lessons taught long ago: finished
    (True, 6, False),    # three more lessons are needed: the group is running again
])
def test_generate_recomputes_is_over(world, was_over, lessons_count, expected):
    from src.services.group_completion_service import compute_is_over

    db, group, monday = world["db"], world["group"], world["monday"]
    start = monday - timedelta(weeks=10)
    group.schedule_config = {
        "start_date": start.isoformat(), "weeks_count": 12, "lessons_count": 5,
        "schedule_items": [{"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 60}],
    }
    group.is_over = was_over
    for week in (0, 1, 2):  # taught eight to ten weeks ago; the grace Wednesday is long past
        _lesson(world, _utc_naive(start + timedelta(weeks=week), 18))
    db.flush()

    _generate(world, start_date=start, lessons_count=lessons_count,
              items=[{"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 60}])

    bounds = [(e.start_datetime, e.end_datetime) for e in _active(world)]
    assert compute_is_over(group.schedule_config, bounds) is expected
    assert group.is_over is expected


# ── (f) the request is validated before anything is written ─────────────────────────


@pytest.fixture
def api(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.config import get_db
    from src.gamification.routes.leaderboard import router
    from src.routes.auth import get_current_user_dependency

    app = FastAPI()
    app.include_router(router, prefix="/leaderboard")
    app.dependency_overrides[get_db] = lambda: world["db"]
    app.dependency_overrides[get_current_user_dependency] = lambda: world["admin"]
    return TestClient(app)


def _body(world, **overrides):
    body = {
        "group_id": world["group"].id,
        "start_date": (world["monday"] + timedelta(days=14)).isoformat(),
        "schedule_items": [{"day_of_week": MON, "time_of_day": "18:00", "duration_minutes": 60}],
        "lessons_count": 2,
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize("overrides", [
    {"schedule_items": [{"day_of_week": 7, "time_of_day": "18:00"}]},
    {"schedule_items": [{"day_of_week": -1, "time_of_day": "18:00"}]},
    {"schedule_items": [{"day_of_week": MON, "time_of_day": "24:00"}]},
    {"schedule_items": [{"day_of_week": MON, "time_of_day": "18:60"}]},
    {"schedule_items": [{"day_of_week": MON, "time_of_day": "1800"}]},
    {"schedule_items": [{"day_of_week": MON, "time_of_day": "18:00:00"}]},
    {"schedule_items": [{"day_of_week": MON, "time_of_day": ""}]},
    {"schedule_items": []},
    {"lessons_count": 0},
    {"lessons_count": 501},
    {"weeks_count": 0},
    {"weeks_count": 53},
], ids=[
    "day_7", "day_negative", "hour_24", "minute_60", "no_colon", "seconds", "empty_time",
    "no_items", "lessons_0", "lessons_501", "weeks_0", "weeks_53",
])
def test_an_invalid_generate_request_is_a_422_and_writes_nothing(world, api, overrides):
    response = api.post("/leaderboard/curator/schedule/generate", json=_body(world, **overrides))

    assert response.status_code == 422, response.text
    assert _active(world) == []
    assert world["group"].schedule_config in (None, {})


def test_a_valid_generate_request_still_goes_through(world, api):
    body = _body(world, weeks_count=52, schedule_items=[
        {"day_of_week": MON, "time_of_day": "00:00", "duration_minutes": 60},
        {"day_of_week": 6, "time_of_day": "23:59"},
    ])

    response = api.post("/leaderboard/curator/schedule/generate", json=body)

    assert response.status_code == 200, response.text
    assert len(_active(world)) == 2
    assert [(i["day_of_week"], i["time_of_day"]) for i in world["group"].schedule_config["schedule_items"]] == [
        (MON, "00:00"), (6, "23:59"),
    ]


def test_the_largest_counts_are_accepted():
    from src.gamification.routes.leaderboard import ScheduleGenerationSchema

    data = ScheduleGenerationSchema(
        group_id=1, start_date=date(2026, 9, 21), lessons_count=500, weeks_count=52,
        schedule_items=[{"day_of_week": 6, "time_of_day": "23:59"}],
    )

    assert (data.lessons_count, data.weeks_count) == (500, 52)
