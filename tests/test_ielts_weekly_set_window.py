"""
The IELTS platform keeps weekly-set windows open past their nominal weekend so
students can finish late — which used to make mid-week probe dates resolve to
LAST week's set and show its stale bands/feedback on the current week's
leaderboard (e.g. week 06.07–12.07 showing the 04.07–05.07 set).

A set now counts for a leaderboard week only if it STARTED inside that week.
The explicitly configured curator-hour date remains a manual override.

Savepoint-isolated (see test_group_week_offset.py for the fixture rationale).
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest

from src.schemas.models import (
    Group, GroupStudent, UserInDB, Event, EventGroup, LeaderboardConfig,
)
from src.gamification.routes.leaderboard import router as leaderboard_router
from src.services.ielts_service import IELTSService


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
        pytest.skip("No database available (requires Postgres); skipping IELTS window tests")

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


# Week 1 starts Monday 2026-06-01; tests view week 2 = [2026-06-08, 2026-06-15).
WEEK1_MONDAY = datetime(2026, 6, 1, 10, 0, 0)
OLD_SET_START = date(2026, 6, 6)       # Saturday of week 1
CURRENT_SET_START = date(2026, 6, 13)  # Saturday of week 2


def _group_with_student(db, suffix):
    admin = _user(db, f"iw_admin_{suffix}@test.local", "admin")
    teacher = _user(db, f"iw_teacher_{suffix}@test.local", "teacher")
    group = Group(name=f"IELTS Win {suffix}", program_type="ielts", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    student = _user(db, f"iw_student_{suffix}@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    for wk in range(2):
        start = WEEK1_MONDAY + timedelta(weeks=wk)
        ev = Event(title=f"c{wk}", event_type="class", start_datetime=start,
                   end_datetime=start + timedelta(hours=1), created_by=teacher.id,
                   is_active=True, is_recurring=False)
        db.add(ev)
        db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id))
    db.flush()
    return admin, group, student


def _item(email, band, feedback):
    return {
        "email": email,
        "listeningBand": band, "readingBand": None,
        "writingBand": None, "speakingBand": None, "overallBand": band,
        "listeningTestName": "L1", "readingTestName": None, "writingTestName": None,
        "listeningFeedback": feedback, "readingFeedback": None,
        "writingFeedback": None, "speakingFeedback": None,
    }


def _fake_fetch(email, current_set_completed):
    """Simulates production: last week's set window stays open through the
    current week's Friday, so mid-week dates resolve to the OLD set."""
    async def fake(emails, date_str):
        d = date.fromisoformat(date_str)
        if d >= CURRENT_SET_START:
            item = _item(email, 7.5, "current-week feedback") if current_set_completed \
                else _item(email, None, None)
            return {
                "weeklySetId": 102, "weeklySetTitle": "13.06 - 14.06",
                "weeklySetDateFrom": "2026-06-13T00:00:00+00:00",
                "results": [item],
            }
        if d >= OLD_SET_START:
            return {
                "weeklySetId": 101, "weeklySetTitle": "06.06 - 07.06",
                "weeklySetDateFrom": "2026-06-06T00:00:00+00:00",
                "results": [_item(email, 6.0, "stale feedback")],
            }
        return {}
    return fake


def _week2_row(db, admin, group):
    result = asyncio.run(_weekly_lessons_endpoint()(
        group.id, week_number=2, current_user=admin, db=db))
    assert len(result["students"]) == 1
    return result["students"][0]


def test_stale_set_not_backfilled(db, monkeypatch):
    """Current set not completed yet ⇒ no IELTS data, not last week's."""
    admin, group, student = _group_with_student(db, "stale")
    monkeypatch.setattr(IELTSService, "fetch_batch_scores_by_date",
                        staticmethod(_fake_fetch(student.email, current_set_completed=False)))

    row = _week2_row(db, admin, group)
    assert row["ielts_overall_band"] is None
    assert row["ielts_weekly_set_title"] is None
    assert row["ielts_listening_feedback"] is None
    assert row["mock_exam"] == 0


def test_current_week_set_shown(db, monkeypatch):
    """A set that started inside the viewed week is accepted as before."""
    admin, group, student = _group_with_student(db, "cur")
    monkeypatch.setattr(IELTSService, "fetch_batch_scores_by_date",
                        staticmethod(_fake_fetch(student.email, current_set_completed=True)))

    row = _week2_row(db, admin, group)
    assert row["ielts_overall_band"] == 7.5
    assert row["ielts_weekly_set_title"] == "13.06 - 14.06"
    assert row["ielts_listening_feedback"] == "current-week feedback"


def test_in_week_curator_hour_date_is_not_a_pin(db, monkeypatch):
    """curator_hour_date inside the viewed week is just the meeting date —
    it must NOT bypass the window filter and re-admit last week's open set."""
    admin, group, student = _group_with_student(db, "inweek")
    db.add(LeaderboardConfig(group_id=group.id, week_number=2,
                             curator_hour_date=date(2026, 6, 10)))  # Wednesday of week 2
    db.flush()
    monkeypatch.setattr(IELTSService, "fetch_batch_scores_by_date",
                        staticmethod(_fake_fetch(student.email, current_set_completed=False)))

    row = _week2_row(db, admin, group)
    assert row["ielts_overall_band"] is None
    assert row["ielts_weekly_set_title"] is None


def test_curator_hour_date_pins_out_of_week_set(db, monkeypatch):
    """An explicitly configured curator-hour date overrides the window filter,
    even though earlier in-week probes rejected (and must not 'seen'-mark) the set."""
    admin, group, student = _group_with_student(db, "pin")
    db.add(LeaderboardConfig(group_id=group.id, week_number=2,
                             curator_hour_date=OLD_SET_START))
    db.flush()
    monkeypatch.setattr(IELTSService, "fetch_batch_scores_by_date",
                        staticmethod(_fake_fetch(student.email, current_set_completed=False)))

    row = _week2_row(db, admin, group)
    assert row["ielts_overall_band"] == 6.0
    assert row["ielts_weekly_set_title"] == "06.06 - 07.06"
    assert row["ielts_listening_feedback"] == "stale feedback"
