from datetime import datetime

import pytest
from fastapi import HTTPException

from src.schemas.models import Step, StepProgressCreateSchema, QuizAttemptCreateSchema
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition, complete_lesson_explicit,
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
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()
    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close(); trans.rollback(); connection.close()


def _world(db):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=2, n_math=1)
    quiz_course, quiz_lesson, quiz_step = make_quiz_lesson(db)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lesson)
    group = make_group(db, enabled=True)
    s = make_user(db)
    enroll(db, s, group, course, admin)
    return admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s


def test_mark_lesson_complete_opens_checkpoint(db):
    from src.progress.routes.progress import mark_lesson_complete
    from src.checkpoints import service
    _, course, v, m, _, _, _, d1, group, s = _world(db)
    complete_lesson_explicit(db, s, course, v[0]); complete_lesson_explicit(db, s, course, v[1])
    out = mark_lesson_complete(m[0].id, time_spent=0, current_user=s, db=db)
    assert [c["number"] for c in out["newly_opened_checkpoints"]] == [1]
    assert service.get_row(db, s.id, group.id, d1.id).status == "available"


def test_step_visit_that_finishes_last_unit_opens_checkpoint(db):
    from src.progress.routes.progress import mark_step_visited
    from src.checkpoints import service
    _, course, v, m, _, _, _, d1, group, s = _world(db)
    complete_lesson_explicit(db, s, course, v[0]); complete_lesson_explicit(db, s, course, v[1])
    steps = db.query(Step).filter(Step.lesson_id == m[0].id).order_by(Step.order_index).all()
    mark_step_visited(steps[0].id, StepProgressCreateSchema(step_id=steps[0].id, time_spent_minutes=1), current_user=s, db=db)
    assert service.get_row(db, s.id, group.id, d1.id) is None
    mark_step_visited(steps[1].id, StepProgressCreateSchema(step_id=steps[1].id, time_spent_minutes=1), current_user=s, db=db)
    assert service.get_row(db, s.id, group.id, d1.id).status == "available"


def _attempt_payload(quiz_course, quiz_lesson, quiz_step, is_draft=False):
    return QuizAttemptCreateSchema(step_id=quiz_step.id, course_id=quiz_course.id, lesson_id=quiz_lesson.id,
                                   quiz_title="Checkpoint 1", total_questions=45, correct_answers=40,
                                   score_percentage=88.89, answers="{}", time_spent_seconds=10,
                                   is_graded=True, is_draft=is_draft, current_question_index=0,
                                   quiz_content_hash=None)


def test_quiz_attempt_gated_and_recorded(db):
    from src.progress.routes.progress import create_quiz_attempt
    from src.checkpoints import service
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    with pytest.raises(HTTPException) as e:
        create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step), current_user=s, db=db)
    assert e.value.status_code == 403
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    attempt = create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step), current_user=s, db=db)
    row = service.get_row(db, s.id, group.id, d1.id)
    assert row.status == "completed" and row.quiz_attempt_id == attempt.id and row.correct_answers == 40


def test_check_course_access_via_checkpoint(db):
    from src.utils.permissions import check_course_access
    from src.checkpoints import service
    admin, course, v, m, quiz_course, _, _, d1, group, s = _world(db)
    assert check_course_access(quiz_course.id, s, db) is False
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    assert check_course_access(quiz_course.id, s, db) is True
