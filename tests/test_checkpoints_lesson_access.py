"""Per-lesson access to the hidden checkpoint quiz course.

`check_course_access` grants the WHOLE hidden course as soon as one checkpoint is open, and every
checkpoint lesson is `is_initially_unlocked=True`, so without a per-lesson guard a student holding
Checkpoint 1 could read Checkpoints 2-9 (questions *and* `correct_answer`) straight off the lesson
and step endpoints.
"""
import pytest
from fastapi import HTTPException

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lessons, make_definition,
)


@pytest.fixture
def db():
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
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


def _world(db):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=4, n_math=2)
    quiz_course, quiz_lessons, quiz_steps = make_quiz_lessons(db, n=2)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lessons[0])
    d2 = make_definition(db, course, 2, v[2:4], m[1], quiz_lessons[1])
    group = make_group(db, enabled=True)
    s = make_user(db)
    enroll(db, s, group, course, admin)
    return admin, group, s, d1, d2, quiz_course, quiz_lessons, quiz_steps


def _open_cp1(db, admin, group, d1):
    from src.checkpoints import service
    service.open_for_students(db, group=group, definition=d1, student_ids=None, actor_id=admin.id)


def test_open_checkpoint_lesson_is_readable(db):
    from src.courses.routes.courses import get_lesson_steps, get_step
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    _open_cp1(db, admin, group, d1)
    assert len(get_lesson_steps(lessons[0].id, include_content=True, current_user=s, db=db)) == 1
    assert get_step(steps[0].id, current_user=s, db=db).id == steps[0].id


def test_other_checkpoint_lesson_is_forbidden(db):
    from src.courses.routes.courses import get_lesson, get_lesson_steps, get_step
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    _open_cp1(db, admin, group, d1)
    for call in (
        lambda: get_lesson(lessons[1].id, current_user=s, db=db),
        lambda: get_lesson_steps(lessons[1].id, include_content=True, current_user=s, db=db),
        lambda: get_step(steps[1].id, current_user=s, db=db),
    ):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 403


def test_check_lesson_access_reports_the_locked_checkpoint(db):
    from src.courses.routes.courses import check_lesson_access
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    _open_cp1(db, admin, group, d1)
    assert check_lesson_access(lessons[0].id, current_user=s, db=db)["accessible"] is True
    out = check_lesson_access(lessons[1].id, current_user=s, db=db)
    assert out["accessible"] is False and out["reason"]


def test_staff_can_read_every_checkpoint_lesson(db):
    from src.courses.routes.courses import get_lesson, get_lesson_steps, get_step
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    _open_cp1(db, admin, group, d1)
    for lesson, step in zip(lessons, steps):
        assert get_lesson(lesson.id, current_user=admin, db=db).id == lesson.id
        assert len(get_lesson_steps(lesson.id, include_content=True, current_user=admin, db=db)) == 1
        assert get_step(step.id, current_user=admin, db=db).id == step.id


def test_non_checkpoint_lessons_are_untouched(db):
    """The guard must only ever fire on a checkpoint quiz lesson."""
    from src.courses.routes.courses import get_lesson_steps
    from src.checkpoints import service
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    _open_cp1(db, admin, group, d1)
    sat_lesson_id = d2.required_units[0].lesson_id
    assert sat_lesson_id not in service.checkpoint_quiz_lesson_ids(db)
    assert get_lesson_steps(sat_lesson_id, include_content=True, current_user=s, db=db) is not None
