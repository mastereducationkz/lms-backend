from datetime import datetime

import pytest
from fastapi import HTTPException

from src.schemas.models import Step, StepProgressCreateSchema, QuizAttemptCreateSchema
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition, complete_lesson_explicit,
)


@pytest.fixture
def db():
    # join_transaction_mode="create_savepoint" (SQLAlchemy 2.0) instead of the older
    # begin_nested()+after_transaction_end-listener recipe: that recipe rebuilds its
    # savepoint by reacting to the transaction the app's own db.commit() just tore down,
    # and a db.rollback() straight after a commit (record_submission failure isolation,
    # see test_record_submission_failure_does_not_500_the_attempt) can unwind past it and
    # silently drop the already-committed row. create_savepoint keeps every app-level
    # commit/rollback nested one level down, on a connection-level transaction this
    # fixture always rolls back, so tests still leave no trace. See tests/onboarding_fixtures.py.
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


def test_non_student_cannot_submit_checkpoint(db):
    from src.progress.routes.progress import create_quiz_attempt
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    with pytest.raises(HTTPException) as e:
        create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step), current_user=admin, db=db)
    assert e.value.status_code == 403


def test_draft_autosave_not_blocked_by_deadline(db):
    from datetime import timedelta
    from src.progress.routes.progress import create_quiz_attempt
    from src.checkpoints import service
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    row = service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id,
                                    now=service.utcnow() - timedelta(days=2))[0]   # deadline already passed
    draft = create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step, is_draft=True), current_user=s, db=db)
    assert draft.is_draft is True
    with pytest.raises(HTTPException) as e:   # final submit is still gated
        create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step), current_user=s, db=db)
    assert e.value.status_code == 409


def test_record_submission_failure_does_not_500_the_attempt(db, monkeypatch):
    from src.progress.routes.progress import create_quiz_attempt
    from src.checkpoints import service
    from src.schemas.models import QuizAttempt
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    def boom(*a, **k):
        raise RuntimeError("simulated checkpoint failure")
    monkeypatch.setattr(service, "record_submission", boom)
    attempt = create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step), current_user=s, db=db)
    assert attempt.id is not None and attempt.is_draft is False
    assert db.query(QuizAttempt).filter_by(user_id=s.id, step_id=quiz_step.id).count() == 1
    assert service.get_row(db, s.id, group.id, d1.id).status == "available"   # bookkeeping failed, row untouched


def test_draft_into_locked_checkpoint_still_403(db):
    from src.progress.routes.progress import create_quiz_attempt
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    with pytest.raises(HTTPException) as e:
        create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step, is_draft=True), current_user=s, db=db)
    assert e.value.status_code == 403


def test_sync_failure_does_not_break_lesson_complete(db, monkeypatch):
    from src.progress.routes import progress as progress_routes
    from src.checkpoints import service
    _, course, v, m, _, _, _, d1, group, s = _world(db)
    complete_lesson_explicit(db, s, course, v[0]); complete_lesson_explicit(db, s, course, v[1])
    def boom(*a, **k):
        raise RuntimeError("simulated checkpoint failure")
    monkeypatch.setattr(service, "sync_student_checkpoints", boom)
    out = progress_routes.mark_lesson_complete(m[0].id, time_spent=0, current_user=s, db=db)
    assert out["detail"] == "Lesson marked as complete" and out["newly_opened_checkpoints"] == []


def _update_payload(**kwargs):
    from src.progress.schemas import QuizAttemptUpdateSchema
    return QuizAttemptUpdateSchema(**kwargs)


def test_patch_finalize_records_checkpoint_submission(db):
    """The web player autosaves a draft (POST) and finalizes via PATCH — that path must record."""
    from src.progress.routes.progress import create_quiz_attempt, update_quiz_attempt
    from src.checkpoints import service
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    draft = create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step, is_draft=True),
                                current_user=s, db=db)
    assert draft.is_draft is True
    final = update_quiz_attempt(draft.id, _update_payload(is_draft=False, correct_answers=40,
                                                          total_questions=45, score_percentage=88.89,
                                                          is_graded=True),
                                current_user=s, db=db)
    assert final.is_draft is False
    row = service.get_row(db, s.id, group.id, d1.id)
    assert row.status == "completed" and row.quiz_attempt_id == final.id and row.correct_answers == 40


def test_patch_finalize_past_deadline_is_rejected(db):
    """Drafts are deadline-exempt, so the deadline must be enforced on the PATCH finalize."""
    from datetime import timedelta
    from src.progress.routes.progress import create_quiz_attempt, update_quiz_attempt
    from src.checkpoints import service
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id,
                              now=service.utcnow() - timedelta(days=2))
    draft = create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step, is_draft=True),
                                current_user=s, db=db)
    with pytest.raises(HTTPException) as e:
        update_quiz_attempt(draft.id, _update_payload(is_draft=False, correct_answers=40),
                            current_user=s, db=db)
    assert e.value.status_code == 409
    assert db.get(type(draft), draft.id).is_draft is True


def test_patch_draft_autosave_not_deadline_gated(db):
    from datetime import timedelta
    from src.progress.routes.progress import create_quiz_attempt, update_quiz_attempt
    from src.checkpoints import service
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id,
                              now=service.utcnow() - timedelta(days=2))
    draft = create_quiz_attempt(_attempt_payload(quiz_course, quiz_lesson, quiz_step, is_draft=True),
                                current_user=s, db=db)
    saved = update_quiz_attempt(draft.id, _update_payload(answers='{"q0": 0}'), current_user=s, db=db)
    assert saved.is_draft is True and saved.answers == '{"q0": 0}'
