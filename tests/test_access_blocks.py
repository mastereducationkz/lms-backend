"""Windows of «no platform access», mirrored from the CRM for the curator grid.

The CRM decides who is blocked (a student who did not renew has their login turned off);
this table only says *when*, so the leaderboard can leave those lessons out of the
attendance denominator the way it already does for freeze days.
"""
from datetime import date, timedelta

import pytest

from src.curator.access_blocks import AccessBlockIndex, StudentAccessBlock, access_block_index
from tests.onboarding_fixtures import db  # noqa: F401 - transactional session fixture


def _row(user_id, start_days_ago, end_days_ago=None, kind="not_renewed"):
    return StudentAccessBlock(
        user_id=user_id,
        blocked_from=date.today() - timedelta(days=start_days_ago),
        blocked_until=None if end_days_ago is None else date.today() - timedelta(days=end_days_ago),
        kind=kind,
    )


def test_an_open_block_covers_from_its_start_onwards():
    index = AccessBlockIndex([_row(7, start_days_ago=5)])
    assert index.is_blocked_on(7, date.today()) is True
    assert index.is_blocked_on(7, date.today() - timedelta(days=5)) is True
    assert index.is_blocked_on(7, date.today() - timedelta(days=6)) is False


def test_a_closed_block_excludes_its_end_day():
    # blocked_until is the day access came back: that day counts again.
    index = AccessBlockIndex([_row(7, start_days_ago=10, end_days_ago=3)])
    assert index.is_blocked_on(7, date.today() - timedelta(days=4)) is True
    assert index.is_blocked_on(7, date.today() - timedelta(days=3)) is False


def test_other_students_and_missing_days_are_never_blocked():
    index = AccessBlockIndex([_row(7, start_days_ago=5)])
    assert index.is_blocked_on(8, date.today()) is False
    assert index.is_blocked_on(7, None) is False


def test_index_loads_rows_for_the_page_in_one_query(db):  # noqa: F811
    db.add_all([_row(101, start_days_ago=2), _row(102, start_days_ago=30, end_days_ago=20)])
    db.commit()
    index = access_block_index(db, [101, 102, 103])
    assert index.is_blocked_on(101, date.today()) is True
    assert index.is_blocked_on(102, date.today()) is False
    assert index.is_blocked_on(102, date.today() - timedelta(days=25)) is True
    assert index.is_blocked_on(103, date.today()) is False


# --- the grid --------------------------------------------------------------------------------
import asyncio
from datetime import datetime

from src.gamification.routes.leaderboard import router as leaderboard_router
from src.schemas.models import Event, EventGroup, Group, GroupStudent, UserInDB


def _endpoint(path):
    for r in leaderboard_router.routes:
        if getattr(r, "path", None) == path:
            return r.endpoint
    raise RuntimeError(f"route {path} not found")


def _maybe_run(result):
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


def _user(db, email, role):
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x", role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


def test_a_lesson_inside_a_block_is_flagged_and_one_before_it_is_not(db):  # noqa: F811
    admin = _user(db, "blk_admin@test.local", "admin")
    teacher = _user(db, "blk_teacher@test.local", "teacher")
    group = Group(name="Blocked grid", program_type="general_english", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    student = _user(db, "blk_student@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=student.id, created_at=datetime(2026, 7, 1)))
    db.flush()
    for day in (14, 16):  # both in the week of 2026-07-13 (Mon) .. 2026-07-19
        start = datetime(2026, 7, day, 10, 0, 0)
        ev = Event(title=f"c{day}", event_type="class", start_datetime=start,
                   end_datetime=start + timedelta(hours=1), created_by=teacher.id,
                   is_active=True, is_recurring=False)
        db.add(ev)
        db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id))
    # Blocked from the 15th: the 14th is theirs, the 16th is not.
    db.add(StudentAccessBlock(user_id=student.id, blocked_from=date(2026, 7, 15), kind="not_renewed"))
    db.flush()

    res = _maybe_run(_endpoint("/curator/weekly-lessons/{group_id}")(
        group.id, week_number=1, current_user=admin, db=db))
    lessons = res["students"][0]["lessons"]
    by_event = {cell["event_id"]: cell for cell in lessons.values()}
    events = {e.title: e for e in db.query(Event).filter(Event.title.in_(["c14", "c16"])).all()}
    assert by_event[events["c14"].id]["blocked"] is False
    assert by_event[events["c16"].id]["blocked"] is True
    # A blocked cell is still not a freeze — the two are rendered differently.
    assert by_event[events["c16"].id]["frozen"] is False
