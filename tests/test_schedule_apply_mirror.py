"""A schedule save counts and pairs lessons exactly as the CRM's does — LMS mirror.

Mirrors ``crm-master/backend/tests/test_schedule_apply_and_preview.py`` for what the LMS has:
``load_schedule_state`` and ``apply_group_schedule`` (the LMS has no preview or card).

``now`` is pinned to this week's Monday 00:00 Almaty, so «last week's two lessons are taught,
this week's are still to come» holds whichever weekday the suite runs on.
"""
from datetime import datetime, time, timedelta, timezone

import pytest

from src.schemas.models import Event, EventGroup, Group, LessonRequest, UserInDB

UTC = timezone.utc
KZ = timezone(timedelta(hours=5))


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


def _now_week_monday():
    today = datetime.now(KZ).date()
    return today - timedelta(days=today.weekday())


@pytest.fixture
def world(db):
    """Two taught lessons last week; old pattern Mon/Wed/Fri 18:00 x 60 for 3 more weeks."""
    teacher = UserInDB(email=f"apply-mirror-{datetime.utcnow().timestamp()}@test.local",
                       name="Teacher", role="teacher", hashed_password="x", is_active=True)
    db.add(teacher); db.flush()
    monday = _now_week_monday()
    old_cfg = {"start_date": (monday - timedelta(days=7)).isoformat(), "lessons_count": 11,
               "schedule_items": [{"day_of_week": d, "time_of_day": "18:00", "duration_minutes": 60}
                                  for d in (0, 2, 4)]}
    group = Group(name="G", teacher_id=teacher.id, is_active=True, schedule_config=old_cfg)
    db.add(group); db.flush()

    def event(start_utc, minutes=60, active=True):
        naive = start_utc.astimezone(UTC).replace(tzinfo=None)
        ev = Event(title="G: Lesson", event_type="class", start_datetime=naive,
                   end_datetime=naive + timedelta(minutes=minutes), is_active=active,
                   created_by=teacher.id, teacher_id=teacher.id)
        db.add(ev); db.flush(); db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
        return ev

    base = datetime.combine(monday - timedelta(days=7), datetime.min.time(), KZ)
    event(base + timedelta(hours=18))              # last Mon, taught
    event(base + timedelta(days=2, hours=18))      # last Wed, taught
    for week in (1, 2, 3):
        for day in (0, 2, 4):
            event(base + timedelta(weeks=week, days=day, hours=18))
    db.commit()
    now = datetime.combine(monday, time.min, KZ).astimezone(UTC)
    return {"db": db, "old": old_cfg, "monday": monday, "now": now, "base": base,
            "group": group, "teacher": teacher, "event": event}


def _new_cfg(world, **overrides):
    """Rauan's shape: Mon/Fri 18:00–19:00, Sat/Sun 19:00–20:30."""
    cfg = {
        "start_date": world["old"]["start_date"],
        "lessons_count": 11,
        "schedule_items": [
            {"day_of_week": 0, "time_of_day": "18:00", "duration_minutes": 60},
            {"day_of_week": 4, "time_of_day": "18:00", "duration_minutes": 60},
            {"day_of_week": 5, "time_of_day": "19:00", "duration_minutes": 90},
            {"day_of_week": 6, "time_of_day": "19:00", "duration_minutes": 90},
        ],
    }
    cfg.update(overrides)
    return cfg


def _class_events(world):
    return sorted(
        world["db"].query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(EventGroup.group_id == world["group"].id, Event.event_type == "class")
        .all(),
        key=lambda e: (e.start_datetime, e.id),
    )


def _active_future(world):
    naive_now = world["now"].astimezone(UTC).replace(tzinfo=None)
    return [e for e in _class_events(world) if e.is_active and e.start_datetime >= naive_now]


def _minutes(ev):
    return int((ev.end_datetime - ev.start_datetime).total_seconds() // 60)


def _apply(world, cfg, previous):
    from src.services.schedule_reconciliation import apply_group_schedule

    db = world["db"]
    result = apply_group_schedule(
        db, world["group"].id, cfg,
        previous_config=previous,
        group_name="G",
        teacher_id=world["teacher"].id,
        created_by=world["teacher"].id,
        fallback_start=None,
        now=world["now"],
    )
    db.commit()
    return result


def _cancel(world, event, status="approved"):
    """What the LMS «отменить урок» leaves behind: the event off and a request naming it."""
    db = world["db"]
    event.is_active = False
    db.add(LessonRequest(request_type="cancel", status=status, event_id=event.id,
                         group_id=world["group"].id, requester_id=world["teacher"].id,
                         original_datetime=event.start_datetime))
    db.commit()


def _state(world):
    from src.services.schedule_reconciliation import load_schedule_state

    return load_schedule_state(world["db"], world["group"].id, world["now"])


def test_the_save_counts_from_lessons_taught_and_keeps_lessons_on_their_slots(world):
    db = world["db"]
    before_ids = {e.id for e in _class_events(world)}
    starts_before = {e.id: e.start_datetime for e in _active_future(world)}

    result = _apply(world, _new_cfg(world), world["old"])

    saved = _active_future(world)
    assert len(saved) == 9  # 11 − 2 taught
    assert sorted(_minutes(e) for e in saved) == [60] * 5 + [90] * 4
    assert {e.id for e in saved} <= before_ids and result["created"] == 0
    # Five lessons already sit on a Monday or Friday slot and stay; the Wednesdays and the
    # Friday past the course's end move onto the weekend.
    kinds = [c.kind for c in result["changes"]]
    assert kinds.count("keep") == 5 and kinds.count("move") == 4
    assert result["deactivated"] == kinds.count("deactivate") == 0
    for change in result["changes"]:
        if change.kind == "keep":
            assert db.get(Event, change.event_id).start_datetime == starts_before[change.event_id]
    assert all(
        e.start_datetime.replace(tzinfo=UTC).astimezone(KZ).weekday() in (5, 6)
        for e in saved if starts_before[e.id] != e.start_datetime
    )
    assert [e.title for e in _class_events(world) if e.is_active] == [f"G: Lesson {i}" for i in range(1, 12)]


def test_state_counts_started_lessons_and_their_minutes(world):
    state = _state(world)

    assert state.started_count == 2
    assert state.started_minutes == 120
    assert len(state.future_events) == 9
    assert state.cancelled_instants == set()


def test_an_approved_cancellation_is_not_resurrected(world):
    db = world["db"]
    naive_wed = (world["base"] + timedelta(weeks=1, days=2, hours=18)).astimezone(UTC).replace(tzinfo=None)
    cancelled = next(e for e in _class_events(world) if e.start_datetime == naive_wed)
    _cancel(world, cancelled)
    cfg = {**world["old"], "lessons_count": world["old"]["lessons_count"] - 1}

    assert _state(world).cancelled_instants == {naive_wed}
    _apply(world, cfg, world["old"])

    saved = _active_future(world)
    assert naive_wed not in {e.start_datetime for e in saved}
    assert db.get(Event, cancelled.id).is_active is False
    assert len(saved) == cfg["lessons_count"] - 2


def test_an_unapproved_cancel_request_does_not_protect_the_slot(world):
    """Only an approved decision removes a lesson for good; a pending one is not a decision."""
    naive_wed = (world["base"] + timedelta(weeks=1, days=2, hours=18)).astimezone(UTC).replace(tzinfo=None)
    _cancel(world, next(e for e in _class_events(world) if e.start_datetime == naive_wed), status="pending")

    assert _state(world).cancelled_instants == set()


def test_a_save_without_a_lesson_count_does_not_resurrect_an_approved_cancellation(world):
    naive_wed = (world["base"] + timedelta(weeks=1, days=2, hours=18)).astimezone(UTC).replace(tzinfo=None)
    _cancel(world, next(e for e in _class_events(world) if e.start_datetime == naive_wed))
    cfg = {k: v for k, v in world["old"].items() if k != "lessons_count"}
    cfg["weeks_count"] = 4  # last week (taught) + the three weeks ahead

    _apply(world, cfg, world["old"])

    saved = _active_future(world)
    assert naive_wed not in {e.start_datetime for e in saved}
    assert len(saved) == 8


def test_a_hand_shortened_lesson_survives_an_unrelated_save(world):
    db = world["db"]
    new_cfg = _new_cfg(world)
    _apply(world, new_cfg, world["old"])

    saturdays = [e for e in _active_future(world)
                 if e.start_datetime.replace(tzinfo=UTC).astimezone(KZ).weekday() == 5]
    last_saturday = saturdays[-1]
    last_saturday.end_datetime = last_saturday.start_datetime + timedelta(minutes=60)
    db.commit()
    lesson_id = last_saturday.id

    same = _new_cfg(world)
    _apply(world, same, new_cfg)
    assert _minutes(db.get(Event, lesson_id)) == 60

    longer = _new_cfg(world)
    longer["schedule_items"][2] = {"day_of_week": 5, "time_of_day": "19:00", "duration_minutes": 120}
    _apply(world, longer, same)
    assert _minutes(db.get(Event, lesson_id)) == 120


def test_the_lessons_a_shorter_course_switches_off_are_the_last_ones(world):
    cfg = {**world["old"], "lessons_count": 9}  # 2 taught + 7: two of the nine ahead go
    last_two = [e.id for e in _active_future(world)][-2:]

    result = _apply(world, cfg, world["old"])

    assert result["deactivated"] == 2
    assert {c.event_id for c in result["changes"] if c.kind == "deactivate"} == set(last_two)
    assert len(_active_future(world)) == 7


def test_a_substituted_lesson_stays_on_its_date_when_another_day_goes(world):
    """Positional pairing carried a substitution pinned to Friday onto the next slot."""
    db = world["db"]
    substitute = UserInDB(email=f"apply-mirror-sub-{datetime.utcnow().timestamp()}@test.local",
                          name="Sub", role="teacher", hashed_password="x", is_active=True)
    db.add(substitute); db.flush()
    friday = next(e for e in _active_future(world)
                  if e.start_datetime.replace(tzinfo=UTC).astimezone(KZ).weekday() == 4)
    friday_start = friday.start_datetime
    friday.teacher_id = substitute.id
    db.add(LessonRequest(request_type="substitution", status="approved", event_id=friday.id,
                         group_id=world["group"].id, requester_id=world["teacher"].id,
                         original_datetime=friday_start, substitute_teacher_id=substitute.id))
    db.commit()

    # Wednesdays go; Mondays and Fridays stay — nine lessons still to come.
    cfg = {**world["old"], "schedule_items": [
        {"day_of_week": 0, "time_of_day": "18:00", "duration_minutes": 60},
        {"day_of_week": 4, "time_of_day": "18:00", "duration_minutes": 60},
    ]}
    _apply(world, cfg, world["old"])

    ev = db.get(Event, friday.id)
    assert (ev.is_active, ev.start_datetime, ev.teacher_id) == (True, friday_start, substitute.id)


def test_legacy_two_item_slots_still_make_hour_long_lessons(world):
    """The bulk import passes ``(dt, lesson number)`` — those groups all ran 60 minutes."""
    from src.services.schedule_reconciliation import reconcile_group_schedule

    db = world["db"]
    # No `now`: the wall clock splits past from future, as the bulk import relies on.
    ahead = [e for e in _class_events(world) if e.is_active and e.start_datetime >= datetime.utcnow()]
    far = world["base"] + timedelta(weeks=8, hours=10)
    result = reconcile_group_schedule(
        db, world["group"].id, [(far, 1), (far + timedelta(days=1), 2, 90)],
        "G", world["teacher"].id, world["teacher"].id,
    )
    db.commit()

    # Every lesson still ahead is on a Mon/Wed/Fri 18:00 that is no longer wanted: the first
    # two move onto the new instants, the rest are switched off.
    assert result["created"] == max(0, 2 - len(ahead))
    assert result["deactivated"] == max(0, len(ahead) - 2)
    made = _active_future(world)[-2:]
    assert [_minutes(e) for e in made] == [60, 90]
    assert made[0].start_datetime == far.astimezone(UTC).replace(tzinfo=None)
    assert made[0].start_datetime.tzinfo is None
