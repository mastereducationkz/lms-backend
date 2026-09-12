"""A head teacher's calendar must use their managed-course scope without crashing."""
from __future__ import annotations

from src.schemas.models import Course, CourseGroupAccess, CourseHeadTeacher
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


def test_head_teacher_calendar_lists_a_lesson_in_a_managed_course(world):
    """Regression for a branch-local CourseGroupAccess import (2026-09-12)."""
    database = world["db"]
    group = world["group"](name="Head teacher's group")
    world["enrol"](group)
    lesson = world["lesson"](group, days_ahead=1)
    head = _user(database, "head_teacher")
    course = Course(title="Head teacher's course", teacher_id=world["teacher"].id, is_active=True)
    database.add(course)
    database.flush()
    database.add(CourseHeadTeacher(course_id=course.id, head_teacher_id=head.id))
    database.add(CourseGroupAccess(
        course_id=course.id,
        group_id=group.id,
        granted_by=world["teacher"].id,
        is_active=True,
    ))
    database.flush()

    from src.events.routes.events import get_calendar_events

    shown = {
        event.id
        for event in get_calendar_events.__wrapped__(
            year=lesson.start_datetime.year,
            month=lesson.start_datetime.month,
            include_finished=False,
            db=database,
            current_user=head,
        )
    }

    assert lesson.id in shown
