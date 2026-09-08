"""GET /courses/lessons/{id}/check-access carries a structured `lock` beside the legacy `reason`.

The web client used to reconstruct "why is this locked, and what do I do" from an error string
plus its own copy of the checkpoint rules. It could never name a unit whose course the student
isn't enrolled in — that lesson appears in no listing the client can fetch — so the guide fell
back to "You can't open this unit yet" with no title. The server knows both, so it says both.

`reason` is deliberately unchanged: existing callers keep reading a plain string.
"""
import pytest
from sqlalchemy.orm import Session as SASession

from src.schemas.models import Course, Module, Lesson
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_definition,
)


@pytest.fixture
def db():
    from sqlalchemy.exc import OperationalError
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close(); trans.rollback(); connection.close()


def _assert_shape(out, kind, title):
    """Every refusal answers 200 with both the legacy string and the structured lock."""
    assert out["accessible"] is False
    assert isinstance(out["reason"], str) and out["reason"]
    lock = out["lock"]
    assert lock["kind"] == kind
    assert lock["unit_title"] == title
    assert lock["reason"] == out["reason"]
    assert lock["steps"] and all(isinstance(s, str) and s for s in lock["steps"])


def test_course_the_student_cannot_see_is_still_named(db):
    """The case the client cannot solve on its own: the lesson is in a course the student has no
    access to, so it is in none of their listings — only the server can supply its title."""
    from src.courses.routes.courses import check_lesson_access
    student = make_user(db)
    other = Course(title="Someone else's course", course_type="general", is_active=True)
    db.add(other); db.flush()
    module = Module(title="Week 8", course_id=other.id, order_index=0)
    db.add(module); db.flush()
    lesson = Lesson(title="More on Modal Verbs", module_id=module.id, order_index=0)
    db.add(lesson); db.flush()

    out = check_lesson_access(lesson.id, current_user=student, db=db)
    _assert_shape(out, "course_access", "More on Modal Verbs")


def test_unit_blocked_by_a_pending_checkpoint_names_it(db):
    """The unit is held back by an open checkpoint: the steps name the checkpoint to take and the
    unit it frees, rather than restating the refusal."""
    from src.courses.routes.courses import check_lesson_access
    from src.checkpoints import service
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=5, n_math=2)
    v[-1].is_initially_unlocked = True
    defs = [make_definition(db, course, i + 1, v[2 * i:2 * i + 2], m[i]) for i in range(2)]
    group = make_group(db, enabled=True)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    db.flush()

    # Block 1's checkpoint is open and unsubmitted, so block 2's units wait on it.
    service.open_for_students(db, group=group, definition=defs[0], student_ids=[student.id],
                              actor_id=admin.id)
    db.flush()
    assert v[2].id in service.blocked_unit_lesson_ids_for_student(db, student.id)

    out = check_lesson_access(v[2].id, current_user=student, db=db)
    _assert_shape(out, "checkpoint_blocked", v[2].title)
    assert any("Checkpoint" in step for step in out["lock"]["steps"])
    assert any(v[2].title in step for step in out["lock"]["steps"])


def test_sequential_lock_does_not_leak_module_and_index_at_the_student(db):
    """The reason used to end with "(Module 7, Index 12)" — internal debugging detail rendered
    verbatim in the student's face."""
    from src.courses.routes.courses import check_lesson_access
    admin = make_user(db, role="admin")
    group = make_group(db, enabled=False)
    student = make_user(db)
    course, verbal, math = make_sat_course(db, n_verbal=3, n_math=1)
    enroll(db, student, group, course, admin)

    out = check_lesson_access(verbal[2].id, current_user=student, db=db)
    _assert_shape(out, "sequential", verbal[2].title)
    assert "Index" not in out["reason"] and "Module " not in out["reason"]
    assert verbal[1].title in out["reason"]


def test_an_open_lesson_carries_no_lock(db):
    from src.courses.routes.courses import check_lesson_access
    admin = make_user(db, role="admin")
    group = make_group(db, enabled=False)
    student = make_user(db)
    course, verbal, math = make_sat_course(db, n_verbal=2, n_math=1)
    enroll(db, student, group, course, admin)

    out = check_lesson_access(verbal[0].id, current_user=student, db=db)
    assert out["accessible"] is True
    assert "lock" not in out
