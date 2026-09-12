"""Attendance is per student; lesson cancellation is an approved, lesson-level action."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.gamification.routes.leaderboard import (
    AttendanceInputSchema,
    BulkAttendanceInputSchema,
    update_attendance,
    update_attendance_bulk,
)
from src.schemas.models import Attendance
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures


def _future_lesson(world):
    group = world["group"](name="Scheduled group")
    student = world["enrol"](group)
    lesson = world["lesson"](group, days_ahead=2)
    return group, student, lesson


def test_attendance_cell_cannot_cancel_a_scheduled_lesson(world):
    group, student, lesson = _future_lesson(world)
    body = BulkAttendanceInputSchema(updates=[AttendanceInputSchema(
        group_id=group.id,
        week_number=1,
        lesson_index=1,
        student_id=student.id,
        score=0,
        status="cancelled",
        event_id=lesson.id,
    )])

    with pytest.raises(HTTPException, match="Отмена относится ко всему уроку"):
        update_attendance_bulk(body, current_user=world["teacher"], db=world["db"])

    assert lesson.is_active is True
    assert world["db"].query(Attendance).filter_by(event_id=lesson.id).count() == 0


def test_single_attendance_cell_cannot_cancel_a_scheduled_lesson(world):
    group, student, lesson = _future_lesson(world)
    body = AttendanceInputSchema(
        group_id=group.id,
        week_number=1,
        lesson_index=1,
        student_id=student.id,
        score=0,
        status="cancelled",
        event_id=lesson.id,
    )

    with pytest.raises(HTTPException, match="Отмена относится ко всему уроку"):
        update_attendance(body, current_user=world["teacher"], db=world["db"])

    assert lesson.is_active is True
    assert world["db"].query(Attendance).filter_by(event_id=lesson.id).count() == 0
