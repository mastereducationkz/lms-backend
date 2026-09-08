"""A group must not close the moment its last lesson STARTS, and not for a while after.

Two defects, one rule.

  * ``is_over`` was computed from ``start_datetime``, so at the instant the final lesson
    began the group counted as finished — teachers lost it off their list mid-lesson while
    attendance still had to be taken. A lesson is behind us only once it has ENDED.
  * Even a genuinely finished group stays open to everyone until the first Wednesday
    23:59:59 Asia/Almaty strictly after its last lesson ends.

The same rule is mirrored in ``crm-master/backend/src/groups/completion.py`` and the two
must agree, so the expected values here are spelled out rather than derived.
"""
from datetime import datetime, time, timedelta

import pytest

from src.schemas.models import Event, EventGroup, Group, UserInDB
from src.services.group_completion_service import (
    ALMATY_UTC_OFFSET,
    GROUP_CLOSE_TIME,
    GROUP_CLOSE_WEEKDAY,
    compute_close_deadline,
    compute_is_over,
    get_groups_close_deadlines,
    get_groups_over_status_changes,
    group_close_deadline,
    sync_groups_over_status,
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


KZ = ALMATY_UTC_OFFSET

_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _kz(year, month, day, hour, minute=0) -> datetime:
    """An Almaty wall-clock instant as the naive UTC the events table stores."""
    return datetime(year, month, day, hour, minute) - KZ


def _pair(start: datetime, minutes: int = 60):
    return (start, start + timedelta(minutes=minutes))


# ── The rule itself, on fixed clocks ──────────────────────────────────────────────────


def test_a_lesson_that_has_started_but_not_ended_keeps_the_group_open():
    """The reported bug: «когда у группы начинается последний урок он сразу уходит в completed»."""
    now = _kz(2026, 9, 9, 19, 30)                       # mid-lesson
    lessons = [_pair(_kz(2026, 9, 2, 19)), _pair(_kz(2026, 9, 9, 19), minutes=90)]

    assert compute_is_over({"lessons_count": 2}, lessons, now) is False
    # Not even a pending close yet — the course is still being taught.
    assert compute_close_deadline({"lessons_count": 2}, lessons, now) is None


def test_a_finished_group_stays_open_until_the_wednesday_cutoff():
    lessons = [_pair(_kz(2026, 9, 2, 19)), _pair(_kz(2026, 9, 3, 19))]   # Wed + Thu
    deadline = _kz(2026, 9, 9, 23, 59) + timedelta(seconds=59)           # next Wednesday

    assert compute_close_deadline({"lessons_count": 2}, lessons, _kz(2026, 9, 4, 12)) == deadline
    # Every moment inside the window: finished, but not closed.
    for moment in (_kz(2026, 9, 3, 20, 1), _kz(2026, 9, 6, 12), _kz(2026, 9, 9, 12)):
        assert compute_is_over({"lessons_count": 2}, lessons, moment) is False


def test_the_cutoff_second_is_the_moment_it_closes():
    lessons = [_pair(_kz(2026, 9, 3, 19))]                               # Thursday
    deadline = _kz(2026, 9, 9, 23, 59) + timedelta(seconds=59)

    assert compute_close_deadline(None, lessons, _kz(2026, 9, 4, 12)) == deadline
    assert compute_is_over(None, lessons, deadline - timedelta(seconds=1)) is False
    assert compute_is_over(None, lessons, deadline) is True
    assert compute_is_over(None, lessons, deadline + timedelta(seconds=1)) is True


def test_a_wednesday_morning_finish_closes_that_same_wednesday_night():
    """The documented reading of «до первой среды 23:59»: the FIRST such instant, not next week."""
    last_end = _kz(2026, 9, 9, 11)                       # Wednesday, lesson 10:00–11:00 Almaty

    assert group_close_deadline(last_end) == _kz(2026, 9, 9, 23, 59) + timedelta(seconds=59)


def test_a_thursday_finish_waits_for_the_following_wednesday():
    last_end = _kz(2026, 9, 10, 11)                      # Thursday

    assert group_close_deadline(last_end) == _kz(2026, 9, 16, 23, 59) + timedelta(seconds=59)


def test_a_wednesday_finish_after_the_cutoff_waits_a_full_week():
    """Strictly after: a lesson still running at 23:59:59 cannot be closed by that instant."""
    last_end = _kz(2026, 9, 9, 23, 59) + timedelta(seconds=59)

    assert group_close_deadline(last_end) == _kz(2026, 9, 16, 23, 59) + timedelta(seconds=59)


def test_a_future_lesson_keeps_the_group_open_whatever_the_cutoff_says():
    lessons = [_pair(_kz(2026, 8, 5, 19)), _pair(_kz(2026, 12, 2, 19))]
    # Long past the Wednesday that would have followed the first lesson.
    now = _kz(2026, 9, 30, 12)

    assert compute_is_over({"lessons_count": 2}, lessons, now) is False
    assert compute_close_deadline({"lessons_count": 2}, lessons, now) is None


def test_the_schedules_lessons_count_still_beats_the_event_count():
    lessons = [_pair(_kz(2026, 9, 1, 19)), _pair(_kz(2026, 9, 3, 19))]
    now = _kz(2026, 9, 30, 12)                            # well past any cutoff

    # Two taught out of eight planned — the plan wins, the group is not finished.
    assert compute_is_over({"lessons_count": 8}, lessons, now) is False
    assert compute_close_deadline({"lessons_count": 8}, lessons, now) is None
    # Without a count, the lessons that exist are the plan.
    assert compute_is_over({}, lessons, now) is True
    # More taught than planned still finishes.
    assert compute_is_over({"lessons_count": 1}, lessons, now) is True


def test_a_null_end_datetime_falls_back_to_start_plus_sixty_minutes():
    start = _kz(2026, 9, 9, 19)
    lessons = [(start, None)]

    # 19:30 Almaty — inside the fallback hour, so the lesson has not ended.
    assert compute_is_over(None, lessons, start + timedelta(minutes=30)) is False
    assert compute_close_deadline(None, lessons, start + timedelta(minutes=30)) is None
    # 20:01 — the fallback end has passed, so a deadline exists, computed from start + 60.
    after = start + timedelta(minutes=61)
    assert compute_close_deadline(None, lessons, after) == group_close_deadline(
        start + timedelta(minutes=60)
    )


def test_the_cutoff_is_configurable_from_the_constants():
    """The school can move the cutoff without a code hunt — assert the wiring, not the value."""
    deadline_local = group_close_deadline(_kz(2026, 9, 10, 11)) + KZ

    assert deadline_local.weekday() == GROUP_CLOSE_WEEKDAY
    assert deadline_local.time() == GROUP_CLOSE_TIME
    assert GROUP_CLOSE_TIME == time(23, 59, 59)


# ── The same rule through the database ────────────────────────────────────────────────


def _teacher(db):
    u = UserInDB(
        email=f"grace-teacher{_uniq()}@test.local", name="Grace Teacher", role="teacher",
        hashed_password=hash_password("x"), is_active=True,
    )
    db.add(u); db.flush(); return u


def _group(db, teacher, *, lessons, lessons_count=None, is_over=False):
    """A group with one class event per ``(start, end)`` pair."""
    config = {"schedule_items": []}
    if lessons_count is not None:
        config["lessons_count"] = lessons_count
    group = Group(
        name=f"Grace G{_uniq()}", is_active=True, is_over=is_over,
        teacher_id=teacher.id, program_type="sat", schedule_config=config,
    )
    db.add(group); db.flush()
    for start, end in lessons:
        ev = Event(
            title=f"{group.name}: Lesson", event_type="class",
            start_datetime=start, end_datetime=end,
            is_active=True, is_online=True, location="Online",
            teacher_id=teacher.id, created_by=teacher.id,
        )
        db.add(ev); db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id))
    db.flush()
    return group


def test_the_group_is_not_closed_while_its_last_lesson_is_being_taught(db):
    """End to end, on the real clock: the class is in the room right now."""
    teacher = _teacher(db)
    now = datetime.utcnow()
    group = _group(db, teacher, lessons_count=2, lessons=[
        (now - timedelta(days=7), now - timedelta(days=7) + timedelta(hours=1)),
        (now - timedelta(minutes=10), now + timedelta(minutes=50)),
    ])

    assert sync_groups_over_status(db, [group.id], commit=False) == 0
    db.refresh(group)
    assert group.is_over is False
    assert get_groups_close_deadlines(db, [group.id]) == {group.id: None}


def test_a_group_inside_its_grace_window_is_open_and_reports_its_close_date(db):
    teacher = _teacher(db)
    now = datetime.utcnow()
    last_end = now - timedelta(minutes=5)
    group = _group(db, teacher, lessons_count=1, lessons=[(last_end - timedelta(hours=1), last_end)])

    assert sync_groups_over_status(db, [group.id], commit=False) == 0
    db.refresh(group)
    assert group.is_over is False

    deadline = get_groups_close_deadlines(db, [group.id])[group.id]
    assert deadline == group_close_deadline(last_end)
    assert deadline > now, "a group still open must close in the future"


def test_a_group_past_its_grace_window_is_closed(db):
    teacher = _teacher(db)
    # Thirty days back is more than the seven-day maximum any grace window can span.
    start = datetime.utcnow() - timedelta(days=30)
    teacher_group = _group(db, teacher, lessons_count=1, lessons=[(start, start + timedelta(hours=1))])

    changes = get_groups_over_status_changes(db, [teacher_group.id])
    assert [(g.id, over) for g, over in changes] == [(teacher_group.id, True)]

    assert sync_groups_over_status(db, [teacher_group.id], commit=False) == 1
    db.refresh(teacher_group)
    assert teacher_group.is_over is True
    assert get_groups_close_deadlines(db, [teacher_group.id])[teacher_group.id] < datetime.utcnow()


def test_a_group_wrongly_flagged_finished_is_reopened(db):
    """The convergence the flag has always done, now with the grace window in the rule."""
    teacher = _teacher(db)
    now = datetime.utcnow()
    group = _group(db, teacher, lessons_count=2, is_over=True, lessons=[
        (now - timedelta(days=7), now - timedelta(days=7) + timedelta(hours=1)),
        (now + timedelta(days=3), now + timedelta(days=3, hours=1)),
    ])

    assert sync_groups_over_status(db, [group.id], commit=False) == 1
    db.refresh(group)
    assert group.is_over is False
