"""
Tests for the per-group weekly-set week offset (aligns leaderboard weeks with the
content-week numbers of NUET weekly sets for groups that start mid-week).

- set_group_week_offset: owner sets/clamps the offset; non-owner is rejected.
- get_weekly_lessons_with_hw_status resolves the NUET set by
  content_week = leaderboard_week − offset.

Savepoint-isolated (the endpoints commit), so nothing persists.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.schemas.models import Group, GroupStudent, UserInDB, Event, EventGroup
from src.gamification.routes.leaderboard import (
    GroupWeekOffsetInputSchema,
    set_group_week_offset,
    router as leaderboard_router,
)
from src.services.sat_service import SATService


# The module defines two functions named get_weekly_lessons_with_hw_status (the second,
# on /curator/leaderboard-full/, shadows the module attribute). The frontend hits the
# first, /curator/weekly-lessons/ — grab it from the router so we test the live one.
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
        pytest.skip("No database available (requires Postgres); skipping offset tests")

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


def _set_offset(db, user, group_id, offset):
    return _maybe_run(
        set_group_week_offset(
            GroupWeekOffsetInputSchema(group_id=group_id, offset=offset),
            current_user=user,
            db=db,
        )
    )


def test_owner_sets_and_clamps_offset(db):
    teacher = _user(db, "off_owner@test.local", "teacher")
    group = Group(name="NUET Off", program_type="nuet", teacher_id=teacher.id)
    db.add(group)
    db.flush()

    res = _set_offset(db, teacher, group.id, 1)
    assert res["weekly_set_week_offset"] == 1
    db.refresh(group)
    assert group.weekly_set_week_offset == 1

    # Clamped to [0, 52]
    assert _set_offset(db, teacher, group.id, 999)["weekly_set_week_offset"] == 52
    assert _set_offset(db, teacher, group.id, -5)["weekly_set_week_offset"] == 0


def test_non_owner_teacher_rejected(db):
    owner = _user(db, "off_owner2@test.local", "teacher")
    intruder = _user(db, "off_intruder@test.local", "teacher")
    group = Group(name="NUET Off2", program_type="nuet", teacher_id=owner.id)
    db.add(group)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        _set_offset(db, intruder, group.id, 3)
    assert exc.value.status_code == 403


def test_nuet_resolution_subtracts_offset(db, monkeypatch):
    admin = _user(db, "off_admin@test.local", "admin")
    teacher = _user(db, "off_res_teacher@test.local", "teacher")
    group = Group(name="NUET Res", program_type="nuet", teacher_id=teacher.id, weekly_set_week_offset=1)
    db.add(group)
    db.flush()

    student = _user(db, "off_res_student@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()

    # Weekly class events across weeks 1–4 (Mondays) so the viewed week has an event
    # (get_weekly_lessons returns early if the requested week has none). 2026-06-01 is a Monday.
    for wk in range(4):
        start = datetime(2026, 6, 1, 10, 0, 0) + timedelta(weeks=wk)
        ev = Event(title=f"c{wk}", event_type="class", start_datetime=start,
                   end_datetime=start + timedelta(hours=1), created_by=teacher.id,
                   is_active=True, is_recurring=False)
        db.add(ev)
        db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id))
    db.flush()

    captured = {}

    async def fake_week(emails, week, exam_type=None):
        captured["week"] = week
        captured["exam_type"] = exam_type
        return {"results": []}

    monkeypatch.setattr(SATService, "fetch_batch_scores_by_week", staticmethod(fake_week))

    # Viewing leaderboard week 3 with offset 1 ⇒ content week 2.
    _maybe_run(_weekly_lessons_endpoint()(
        group.id, week_number=3, current_user=admin, db=db))
    assert captured.get("exam_type") == "NUET"
    assert captured.get("week") == 2


def _maybe_run(_result):
    """Handlers converted from async def to def now return values directly; still run coroutines
    for any endpoint that remained async."""
    import asyncio as _asyncio
    return _asyncio.run(_result) if _asyncio.iscoroutine(_result) else _result
