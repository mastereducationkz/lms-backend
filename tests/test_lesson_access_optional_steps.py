"""check_lesson_access must ignore optional steps, matching the modules-list badge.

Regression tests for: a student completes all required steps of a lesson (the
modules list shows it green/completed because it excludes ``is_optional`` steps),
but GET /lessons/{id}/check-access blocks the next lesson with "Please complete
the previous lesson" because the gate counted optional steps too.

Same real-Postgres SAVEPOINT fixture as tests/test_favorite_steps.py.
"""
import pytest

from src.schemas.models import (
    Course, CourseGroupAccess, Group, GroupStudent, Lesson, Module, Step,
    StepProgress, UserInDB,
)
from src.courses.routes.courses import check_lesson_access


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
    """Student in a non-special group with access to a two-lesson course.

    Lesson 1 mirrors prod Unit 18: required steps, optional quiz steps, and a
    summary step whose is_optional is NULL (NULL counts as required).
    """
    student = UserInDB(email="seq-student@test.local", name="Seq Student",
                       hashed_password="x", role="student", is_active=True)
    admin = UserInDB(email="seq-admin@test.local", name="Seq Admin",
                     hashed_password="x", role="admin", is_active=True)
    db.add_all([student, admin]); db.flush()

    group = Group(name="Seq Group", is_special=False)
    db.add(group); db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))

    course = Course(title="Seq Course", is_active=True, release_schedule="all")
    db.add(course); db.flush()
    db.add(CourseGroupAccess(course_id=course.id, group_id=group.id,
                             granted_by=admin.id, is_active=True))

    module = Module(course_id=course.id, title="Module 1", order_index=0)
    db.add(module); db.flush()

    lesson1 = Lesson(module_id=module.id, title="Unit 18", order_index=0)
    lesson2 = Lesson(module_id=module.id, title="Unit 19", order_index=1)
    db.add_all([lesson1, lesson2]); db.flush()

    req1 = Step(lesson_id=lesson1.id, title="Step 1", content_type="text",
                order_index=1, is_optional=False)
    opt = Step(lesson_id=lesson1.id, title="Step 2 (optional quiz)", content_type="quiz",
               order_index=2, is_optional=True)
    summary = Step(lesson_id=lesson1.id, title="Lesson Summary", content_type="summary",
                   order_index=3, is_optional=None)
    l2_step = Step(lesson_id=lesson2.id, title="Step 1", content_type="text",
                   order_index=1, is_optional=False)
    l2_opt = Step(lesson_id=lesson2.id, title="Step 2 (optional quiz)", content_type="quiz",
                  order_index=2, is_optional=True)
    db.add_all([req1, opt, summary, l2_step, l2_opt]); db.flush()

    return {
        "db": db, "student": student, "course": course, "module": module,
        "lesson1": lesson1, "lesson2": lesson2,
        "req1": req1, "opt": opt, "summary": summary, "l2_step": l2_step,
    }


def _complete(db, user, course, steps):
    for s in steps:
        db.add(StepProgress(user_id=user.id, course_id=course.id,
                            lesson_id=s.lesson_id, step_id=s.id, status="completed"))
    db.flush()


def test_incomplete_optional_step_does_not_block_next_lesson(env):
    """All required steps of lesson 1 done, optional quiz skipped → lesson 2 opens."""
    db = env["db"]
    _complete(db, env["student"], env["course"], [env["req1"], env["summary"]])

    result = check_lesson_access(env["lesson2"].id, current_user=env["student"], db=db)

    assert result["accessible"] is True, result.get("reason")


def test_incomplete_required_step_still_blocks_next_lesson(env):
    """Summary step (is_optional NULL) unfinished → lesson 2 stays locked."""
    db = env["db"]
    _complete(db, env["student"], env["course"], [env["req1"], env["opt"]])

    result = check_lesson_access(env["lesson2"].id, current_user=env["student"], db=db)

    assert result["accessible"] is False


def test_revisiting_lesson_completed_via_required_steps(env):
    """A lesson whose required steps are all done is re-visitable even when the
    previous lesson is untouched (e.g. the student was skipped ahead)."""
    db = env["db"]
    _complete(db, env["student"], env["course"], [env["l2_step"]])

    result = check_lesson_access(env["lesson2"].id, current_user=env["student"], db=db)

    assert result["accessible"] is True, result.get("reason")


def test_redirect_source_completed_via_required_steps(env):
    """lesson1 redirects to lesson2; required steps of lesson1 done, optional
    skipped → lesson2 unlocks through the redirect branch."""
    db = env["db"]
    env["lesson1"].next_lesson_id = env["lesson2"].id
    db.flush()
    _complete(db, env["student"], env["course"], [env["req1"], env["summary"]])

    result = check_lesson_access(env["lesson2"].id, current_user=env["student"], db=db)

    assert result["accessible"] is True, result.get("reason")
