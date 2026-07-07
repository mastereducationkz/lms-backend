"""
Equivalence test for the N+1 fix in teacher students-progress.

calculate_module_progress_for_students() batches the course structure lookup
across many students in one shot, but must produce byte-for-byte identical
per-student results to the original calculate_student_module_progress().

This test seeds a small course (2 modules, a handful of lessons, a mix of
required/optional steps) and 3 students with different StepProgress /
StudentProgress states, then asserts the batched function's output for each
student matches the original single-student function's output exactly.

Uses a real Postgres session (SessionLocal) since the models rely on
Postgres-only column types (JSONB) that don't work against SQLite. All seed
data is created inside one transaction and rolled back at the end so the
test leaves no trace in the database.
"""
from datetime import datetime, timezone

import pytest

from src.config import SessionLocal
from src.schemas.models import (
    Course,
    Lesson,
    Module,
    Step,
    StepProgress,
    StudentProgress,
    UserInDB,
)
from src.progress.services.lesson_completion import (
    calculate_module_progress_for_students,
    calculate_student_module_progress,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()  # discard everything seeded by the test
        session.close()


def _make_user(db, email):
    user = UserInDB(
        email=email,
        name=email.split("@")[0],
        hashed_password="x",
        role="student",
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def test_batched_progress_matches_single_student_function(db):
    course = Course(title="Batch Test Course", is_active=True)
    db.add(course)
    db.flush()

    students = [_make_user(db, f"batch-test-student-{i}@example.com") for i in range(3)]
    db.flush()

    # Module 1: two lessons.
    #   Lesson 1: 2 required steps + 1 optional step.
    #   Lesson 2: 0 required steps, 2 steps total (all-optional fallback).
    # Module 2: one lesson.
    #   Lesson 3: 2 required steps.
    module1 = Module(course_id=course.id, title="Module 1", order_index=0)
    module2 = Module(course_id=course.id, title="Module 2", order_index=1)
    db.add_all([module1, module2])
    db.flush()

    lesson1 = Lesson(module_id=module1.id, title="Lesson 1", order_index=0)
    lesson2 = Lesson(module_id=module1.id, title="Lesson 2", order_index=1)
    lesson3 = Lesson(module_id=module2.id, title="Lesson 3", order_index=0)
    db.add_all([lesson1, lesson2, lesson3])
    db.flush()

    steps = [
        Step(lesson_id=lesson1.id, title="L1S1", content_type="text", order_index=0, is_optional=False),
        Step(lesson_id=lesson1.id, title="L1S2", content_type="text", order_index=1, is_optional=False),
        Step(lesson_id=lesson1.id, title="L1S3", content_type="text", order_index=2, is_optional=True),
        Step(lesson_id=lesson2.id, title="L2S1", content_type="text", order_index=0, is_optional=True),
        Step(lesson_id=lesson2.id, title="L2S2", content_type="text", order_index=1, is_optional=True),
        Step(lesson_id=lesson3.id, title="L3S1", content_type="text", order_index=0, is_optional=False),
        Step(lesson_id=lesson3.id, title="L3S2", content_type="text", order_index=1, is_optional=False),
    ]
    db.add_all(steps)
    db.flush()
    steps_by_key = {(s.lesson_id, s.title): s for s in steps}

    now = datetime.now(timezone.utc)

    def complete(user_id, course_id, lesson_id, step_id):
        db.add(StepProgress(
            user_id=user_id,
            course_id=course_id,
            lesson_id=lesson_id,
            step_id=step_id,
            status="completed",
            visited_at=now,
            completed_at=now,
        ))

    s0, s1, s2 = students[0].id, students[1].id, students[2].id

    # Student 0: completes lesson 1 fully (both required steps), nothing else.
    complete(s0, course.id, lesson1.id, steps_by_key[(lesson1.id, "L1S1")].id)
    complete(s0, course.id, lesson1.id, steps_by_key[(lesson1.id, "L1S2")].id)

    # Student 1: completes lesson 1 partially (1/2 required steps -> "current
    # lesson"), and completes lesson 2 via the all-optional fallback (1/2
    # steps, not enough -> still current if it were first incomplete, but
    # lesson 1 is first incomplete since it's earlier in the traversal order).
    complete(s1, course.id, lesson1.id, steps_by_key[(lesson1.id, "L1S1")].id)
    complete(s1, course.id, lesson2.id, steps_by_key[(lesson2.id, "L2S1")].id)

    # Student 2: completes everything (lesson1, lesson2 via StudentProgress
    # override instead of steps, and lesson3), to exercise the
    # completed_lesson_ids fallback path plus "no incomplete -> last lesson
    # at 100%".
    complete(s2, course.id, lesson1.id, steps_by_key[(lesson1.id, "L1S1")].id)
    complete(s2, course.id, lesson1.id, steps_by_key[(lesson1.id, "L1S2")].id)
    db.add(StudentProgress(
        user_id=s2,
        course_id=course.id,
        lesson_id=lesson2.id,
        status="completed",
        completion_percentage=100,
        last_accessed=now,
        completed_at=now,
    ))
    complete(s2, course.id, lesson3.id, steps_by_key[(lesson3.id, "L3S1")].id)
    complete(s2, course.id, lesson3.id, steps_by_key[(lesson3.id, "L3S2")].id)

    db.flush()

    student_ids = [s0, s1, s2]
    batched = calculate_module_progress_for_students(db, student_ids, course.id)

    assert set(batched.keys()) == set(student_ids)

    for sid in student_ids:
        single = calculate_student_module_progress(db, sid, course.id)
        assert batched[sid] == single, f"mismatch for student {sid}: batched={batched[sid]} single={single}"

    print("Batched results:")
    for sid in student_ids:
        print(f"  student {sid}: {batched[sid]}")
