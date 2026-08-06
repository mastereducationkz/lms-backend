"""Curator leaderboard must expose ALL homeworks of a lesson, not just one.

Regression tests for: a lesson day with two assignments sharing the same
lesson_number showed only one of them in the grid (dict last-wins), so a student
who submitted the other one displayed as "Не выполнено".

Same real-Postgres SAVEPOINT fixture as tests/test_favorite_steps.py.
"""
import asyncio
from datetime import datetime

import pytest

from src.schemas.models import (
    Assignment, AssignmentSubmission, Event, EventGroup, Group, GroupStudent,
    UserInDB,
)
from src.gamification.routes.leaderboard import get_weekly_lessons_with_hw_status


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
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


@pytest.fixture
def env(db):
    """Group with one student and two class events in week 1; the first lesson
    has TWO homeworks — the student submitted only the second one."""
    admin = UserInDB(email="lb-admin@test.local", name="LB Admin",
                     hashed_password="x", role="admin", is_active=True)
    student = UserInDB(email="lb-student@test.local", name="LB Student",
                       hashed_password="x", role="student", is_active=True)
    db.add_all([admin, student]); db.flush()

    group = Group(name="Leaderboard Group", program_type="general_english",
                  is_special=False, is_active=True)
    db.add(group); db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))

    # Monday and Tuesday of the same week -> both are week 1
    ev1 = Event(title="Lesson 1", event_type="class",
                start_datetime=datetime(2026, 3, 2, 11, 0),
                end_datetime=datetime(2026, 3, 2, 12, 0),
                created_by=admin.id, is_active=True, is_recurring=False)
    ev2 = Event(title="Lesson 2", event_type="class",
                start_datetime=datetime(2026, 3, 3, 11, 0),
                end_datetime=datetime(2026, 3, 3, 12, 0),
                created_by=admin.id, is_active=True, is_recurring=False)
    db.add_all([ev1, ev2]); db.flush()
    db.add_all([EventGroup(event_id=ev1.id, group_id=group.id),
                EventGroup(event_id=ev2.id, group_id=group.id)])

    hw_a = Assignment(group_id=group.id, lesson_number=1, title="math lesson 8",
                      assignment_type="quiz", content="{}", max_score=50,
                      due_date=datetime(2026, 3, 2, 11, 0), is_active=True)
    hw_b = Assignment(group_id=group.id, lesson_number=1, title="verbal lesson 14",
                      assignment_type="quiz", content="{}", max_score=40,
                      due_date=datetime(2026, 3, 2, 11, 0), is_active=True)
    db.add_all([hw_a, hw_b]); db.flush()

    # Student submitted ONLY hw_b (the one the old last-wins map could hide)
    db.add(AssignmentSubmission(assignment_id=hw_b.id, user_id=student.id,
                                answers="{}", score=40, max_score=40,
                                is_graded=True,
                                submitted_at=datetime(2026, 3, 4, 10, 0)))
    db.flush()

    return {"db": db, "admin": admin, "student": student, "group": group,
            "hw_a": hw_a, "hw_b": hw_b}


def _call(env):
    return asyncio.run(get_weekly_lessons_with_hw_status(
        env["group"].id, week_number=1,
        current_user=env["admin"], db=env["db"],
    ))


def test_lesson_meta_lists_all_homeworks(env):
    result = _call(env)

    lesson1 = next(l for l in result["lessons"] if l["lesson_number"] == 1)
    titles = [hw["title"] for hw in lesson1["homeworks"]]
    assert titles == ["math lesson 8", "verbal lesson 14"]
    # legacy single-homework field still present for older clients
    assert lesson1["homework"] is not None


def test_student_gets_status_for_every_homework(env):
    result = _call(env)

    row = result["students"][0]
    statuses = row["lessons"]["1"]["homework_statuses"]
    by_id = {s["assignment_id"]: s for s in statuses}

    assert by_id[env["hw_a"].id]["submitted"] is False
    assert by_id[env["hw_b"].id]["submitted"] is True
    assert by_id[env["hw_b"].id]["score"] == 40
    assert by_id[env["hw_b"].id]["late"] is True


def test_legacy_single_status_prefers_submitted_homework(env):
    """Older cached frontends read homework_status (single); it must show the
    submitted homework rather than an arbitrary unsubmitted twin."""
    result = _call(env)

    row = result["students"][0]
    legacy = row["lessons"]["1"]["homework_status"]
    assert legacy["submitted"] is True
    assert legacy["score"] == 40
