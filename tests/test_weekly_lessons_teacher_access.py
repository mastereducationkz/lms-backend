"""Teacher access to the curator-leaderboard weekly-lessons grid.

Teachers need the same weekly-lessons grid curators/head_teachers see (read
only), including the `activity_score` per lesson entry and the lesson
`topic` in the column meta — the frontend leaderboard-parity task depends on
both fields being present. Teachers must NOT be able to write manual scores
or leaderboard config (pinned by test_teacher_403_on_manual_scores_and_config).

Same real-Postgres SAVEPOINT fixture as tests/test_leaderboard_multi_homework.py.
"""
import asyncio

import pytest
from datetime import datetime

from fastapi import HTTPException

from src.schemas.models import Attendance, Event, EventGroup, Group, GroupStudent, UserInDB
from src.gamification.routes.leaderboard import (
    get_weekly_lessons_with_hw_status,
    update_leaderboard_entry,
    update_leaderboard_config,
)
from src.gamification.schemas import (
    LeaderboardConfigUpdateSchema,
    LeaderboardEntryCreateSchema,
)


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


def _make_teacher(db, email, name):
    teacher = UserInDB(email=email, name=name, hashed_password="x",
                        role="teacher", is_active=True)
    db.add(teacher)
    db.flush()
    return teacher


def _make_group_with_lesson(db, teacher, activity_score=7.0):
    """Group owned by teacher (teacher_id), one student, one class event in
    week 1 with an Attendance row (status attended, activity_score set) and
    a topic on the event."""
    student = UserInDB(email="wl-student@test.local", name="WL Student",
                        hashed_password="x", role="student", is_active=True)
    db.add(student)
    db.flush()

    group = Group(name="Weekly Lessons Group", program_type="general_english",
                  is_special=False, is_active=True, teacher_id=teacher.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))

    event = Event(title="Lesson 1", event_type="class", topic="Present Perfect",
                  start_datetime=datetime(2026, 3, 2, 11, 0),
                  end_datetime=datetime(2026, 3, 2, 12, 0),
                  created_by=teacher.id, is_active=True, is_recurring=False)
    db.add(event)
    db.flush()
    db.add(EventGroup(event_id=event.id, group_id=group.id))

    db.add(Attendance(event_id=event.id, user_id=student.id, status="present",
                       activity_score=activity_score))
    db.flush()

    return teacher, group, student, event


def test_teacher_gets_weekly_lessons_with_activity_and_topic(db):
    teacher = _make_teacher(db, "wl-teacher@test.local", "WL Teacher")
    teacher, group, student, event = _make_group_with_lesson(db, teacher)

    result = asyncio.run(get_weekly_lessons_with_hw_status(
        group.id, week_number=1, current_user=teacher, db=db,
    ))

    assert result["lessons"], "teacher should receive lesson columns"
    assert result["lessons"][0]["topic"] == "Present Perfect"

    row = result["students"][0]
    lesson_entry = list(row["lessons"].values())[0]
    assert lesson_entry["activity_score"] == 7.0


def test_foreign_teacher_gets_403(db):
    teacher = _make_teacher(db, "wl-owner@test.local", "WL Owner")
    teacher, group, student, event = _make_group_with_lesson(db, teacher)
    outsider = _make_teacher(db, "wl-outsider@test.local", "WL Outsider")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_weekly_lessons_with_hw_status(
            group.id, week_number=1, current_user=outsider, db=db,
        ))
    assert exc.value.status_code == 403


def test_teacher_403_on_manual_scores_and_config(db):
    teacher = _make_teacher(db, "wl-teacher2@test.local", "WL Teacher2")
    teacher, group, student, event = _make_group_with_lesson(db, teacher)

    with pytest.raises(HTTPException) as exc:
        update_leaderboard_entry(
            LeaderboardEntryCreateSchema(
                user_id=student.id, group_id=group.id, week_number=1, extra_points=5,
            ),
            current_user=teacher, db=db,
        )
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        update_leaderboard_config(
            LeaderboardConfigUpdateSchema(group_id=group.id, week_number=1,
                                          extra_points_enabled=True),
            current_user=teacher, db=db,
        )
    assert exc.value.status_code == 403
