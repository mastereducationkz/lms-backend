"""Tests for the Weekly Top Students composite leaderboard.

Two layers:
- Pure scoring-function tests (no DB) that pin the homework formula, the
  N/A-homework weight renormalization, and the percentile normalization.
- One integration test against a real Postgres session (seeded + rolled back,
  matching the repo convention) that exercises the full query/merge/score flow,
  scoped to a freshly created group so it never touches production rows.
"""
from datetime import datetime, timezone

import pytest

from src.services.weekly_top_students_service import (
    WEIGHTS,
    _composite,
    _homework_subscore,
    _norm,
    _percentile,
    resolve_week,
    compute_weekly_top_students,
)


# --------------------------------------------------------------------------- #
# Pure scoring functions
# --------------------------------------------------------------------------- #

def test_homework_subscore_full_on_time_graded():
    # due=2, all on-time+submitted, avg 100% -> 50 + 30 + 20 = 100
    h = {"due": 2, "submitted": 2, "on_time": 2, "avg_pct": 100.0}
    assert _homework_subscore(h) == 100.0


def test_homework_subscore_ungraded_fallback():
    # Nothing graded -> 20% term dropped, split renormalizes to 62.5 / 37.5.
    # on_time 1/2, submitted 2/2 -> 62.5*0.5 + 37.5*1 = 68.75
    h = {"due": 2, "submitted": 2, "on_time": 1, "avg_pct": None}
    assert _homework_subscore(h) == pytest.approx(68.75)


def test_homework_subscore_none_when_nothing_due():
    assert _homework_subscore({"due": 0, "submitted": 0, "on_time": 0, "avg_pct": None}) is None


def test_homework_subscore_capped_at_100():
    # avg_pct can exceed 100 (bonus scoring); subscore stays clamped.
    h = {"due": 1, "submitted": 1, "on_time": 1, "avg_pct": 125.0}
    assert _homework_subscore(h) == 100.0


def test_composite_all_dimensions():
    got = _composite(80.0, 60.0, 40.0, 20.0)
    expected = (
        80.0 * WEIGHTS["homework"]
        + 60.0 * WEIGHTS["course"]
        + 40.0 * WEIGHTS["study"]
        + 20.0 * WEIGHTS["engagement"]
    )  # weights already sum to 1.0
    assert got == pytest.approx(expected)


def test_composite_homework_na_counts_as_zero():
    # HW None -> homework slice contributes 0 (no renormalization).
    got = _composite(None, 100.0, 0.0, 0.0)
    assert got == pytest.approx(100.0 * WEIGHTS["course"])


def test_composite_no_homework_caps_below_full():
    # Perfect on every other dimension but no homework -> capped at ~65/100,
    # so a no-homework student cannot outrank a full-homework student.
    got = _composite(None, 100.0, 100.0, 100.0)
    assert got == pytest.approx(65.0)
    assert got < _composite(1.0, 100.0, 100.0, 100.0)


def test_composite_zero_when_nothing():
    assert _composite(None, 0.0, 0.0, 0.0) == 0.0


def test_percentile_and_norm():
    assert _percentile([], 0.95) == 0.0
    assert _percentile([42.0], 0.95) == 42.0
    assert _percentile([0, 10, 20, 30, 100], 0.95) == 100
    assert _norm(50, 100) == 50.0
    assert _norm(200, 100) == 100.0  # clamped
    assert _norm(5, 0) == 0.0        # cap 0 -> guard


def test_resolve_week_snaps_to_monday():
    from datetime import date
    # 2026-06-10 is a Wednesday; Monday of that ISO week is 2026-06-08.
    monday, sunday = resolve_week(date(2026, 6, 10))
    assert monday == date(2026, 6, 8)
    assert sunday == date(2026, 6, 14)


# --------------------------------------------------------------------------- #
# Integration (real Postgres, seeded + rolled back, scoped to a fresh group)
# --------------------------------------------------------------------------- #

@pytest.fixture
def db():
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from src.config import SessionLocal

    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
    except OperationalError:
        session.close()
        pytest.skip("No database available (requires Postgres); skipping integration test")
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_compute_weekly_top_students_integration(db):
    from datetime import date
    from src.schemas.models import (
        UserInDB, Group, GroupStudent, Course, Module, Lesson, Step,
        StepProgress, Assignment, AssignmentSubmission, PointHistory,
    )

    # A weekday inside the Almaty week starting Mon 2026-06-08.
    in_week = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)

    def user(email, **kw):
        u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x", **kw)
        db.add(u)
        db.flush()
        return u

    teacher = user("wts-teacher@example.com", role="teacher")
    curator = user("wts-curator@example.com", role="curator")
    stu_a = user("wts-a@example.com", role="student", is_active=True, daily_streak=7)
    stu_b = user("wts-b@example.com", role="student", is_active=True, daily_streak=0)
    stu_hidden = user("wts-hidden@example.com", role="student", is_active=True, is_analytics_hidden=True)

    group = Group(
        name="WTS Test Group", teacher_id=teacher.id, curator_id=curator.id,
        is_active=True, is_over=False, program_type="sat",
    )
    db.add(group)
    db.flush()
    for s in (stu_a, stu_b, stu_hidden):
        db.add(GroupStudent(group_id=group.id, student_id=s.id))
    db.flush()

    course = Course(title="WTS Course", is_active=True)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="M1", order_index=0)
    db.add(module)
    db.flush()
    lesson = Lesson(module_id=module.id, title="L1", order_index=0)
    db.add(lesson)
    db.flush()
    steps = [
        Step(lesson_id=lesson.id, title=f"S{i}", content_type="text", order_index=i, is_optional=False)
        for i in range(3)
    ]
    db.add_all(steps)
    db.flush()

    # One assignment due this week, scoped to the group (assignments.group_id +
    # assignments.due_date, as production stores deadlined homework).
    assignment = Assignment(
        lesson_id=lesson.id, group_id=group.id, title="HW1", assignment_type="file",
        content="x", max_score=100, due_date=in_week, is_active=True, is_hidden=False,
    )
    db.add(assignment)
    db.flush()

    # Student A: submits on time, graded 100% -> HW subscore 100.
    db.add(AssignmentSubmission(
        assignment_id=assignment.id, user_id=stu_a.id, answers="x", max_score=100,
        score=100, is_graded=True, is_late=False, is_hidden=False, submitted_at=in_week,
    ))
    # Student B: no submission -> missing -> needs_attention.

    # Steps this week: A completes 3, B completes 1.
    def complete(uid, step):
        db.add(StepProgress(
            user_id=uid, course_id=course.id, lesson_id=lesson.id, step_id=step.id,
            status="completed", visited_at=in_week, completed_at=in_week, time_spent_minutes=10,
        ))
    for st in steps:
        complete(stu_a.id, st)
    complete(stu_b.id, steps[0])

    # Points this week: A 100, B 10.
    db.add(PointHistory(user_id=stu_a.id, amount=100, reason="test", created_at=in_week))
    db.add(PointHistory(user_id=stu_b.id, amount=10, reason="test", created_at=in_week))
    db.flush()

    result = compute_weekly_top_students(db, date(2026, 6, 8), group_id=group.id, limit=50)

    assert result["week_start"] == "2026-06-08"
    assert result["total_students"] == 2  # hidden student excluded

    ids = [r["student_id"] for r in result["students"]]
    assert stu_hidden.id not in ids
    assert set(ids) == {stu_a.id, stu_b.id}

    by_id = {r["student_id"]: r for r in result["students"]}
    a = by_id[stu_a.id]
    assert a["rank"] == 1  # A dominates every dimension
    assert a["homework"]["subscore"] == 100.0
    assert a["homework"]["on_time"] == 1 and a["homework"]["due"] == 1
    assert a["activity_score"] >= by_id[stu_b.id]["activity_score"]

    # B has a missing assignment -> appears in needs_attention.
    na_ids = [r["student_id"] for r in result["needs_attention"]]
    assert stu_b.id in na_ids

    # Program filter that doesn't match the group yields an empty cohort.
    empty = compute_weekly_top_students(db, date(2026, 6, 8), group_id=group.id, program_type="ielts")
    assert empty["students"] == []
