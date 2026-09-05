from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.schemas.models import QuizAttempt
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition, complete_lesson_explicit,
)


@pytest.fixture
def db():
    # join_transaction_mode="create_savepoint" (SQLAlchemy 2.0) instead of the older
    # begin_nested()+after_transaction_end-listener recipe: that recipe rebuilds its
    # savepoint by reacting to the transaction the app's own db.commit() just tore down,
    # and a db.rollback() straight after a commit (record_submission failure isolation,
    # see test_checkpoints_hooks.py::test_record_submission_failure_does_not_500_the_attempt)
    # can unwind past it and silently drop the already-committed row. create_savepoint keeps
    # every app-level commit/rollback nested one level down, on a connection-level transaction
    # this fixture always rolls back, so tests still leave no trace. See tests/onboarding_fixtures.py.
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
    s1, s2 = make_user(db), make_user(db)
    enroll(db, s1, group, course, admin); enroll(db, s2, group, course, admin)
    return admin, course, quiz_course, quiz_lesson, quiz_step, d1, group, s1, s2


def _attempt(db, student, quiz_course, quiz_lesson, quiz_step, correct=30, total=45):
    a = QuizAttempt(user_id=student.id, step_id=quiz_step.id, course_id=quiz_course.id,
                    lesson_id=quiz_lesson.id, total_questions=total, correct_answers=correct,
                    score_percentage=round(correct * 100 / total, 2), is_draft=False,
                    completed_at=datetime(2026, 9, 11, 10, 0))
    db.add(a); db.flush()
    return a


def test_open_for_whole_group_then_single_student_reopen(db):
    from src.checkpoints import service
    admin, _, _, _, _, d1, group, s1, s2 = _world(db)
    now = datetime(2026, 9, 10, 18, 0)
    rows = service.open_for_students(db, group=group, definition=d1, actor_id=admin.id, now=now)
    assert {r.student_id for r in rows} == {s1.id, s2.id}
    assert all(r.status == "available" and r.opened_by == "admin"
               and r.deadline == now + timedelta(hours=service.DEADLINE_HOURS) for r in rows)
    # open again is a no-op for already-open rows
    assert service.open_for_students(db, group=group, definition=d1, actor_id=admin.id, now=now) == []
    later = now + timedelta(days=3)
    re = service.reopen_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id, now=later)
    assert len(re) == 1 and re[0].student_id == s1.id and re[0].status == "reopened"
    assert re[0].reopen_count == 1 and re[0].deadline == later + timedelta(hours=service.DEADLINE_HOURS)
    assert service.get_row(db, s2.id, group.id, d1.id).status == "available"


def test_set_deadline_revives_overdue(db):
    from src.checkpoints import service
    admin, _, _, _, _, d1, group, s1, _ = _world(db)
    now = datetime(2026, 9, 12, 12, 0)
    row = service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id,
                                    now=now - timedelta(hours=service.DEADLINE_HOURS + 24))[0]
    service.refresh_overdue([row], now)
    assert row.status == "overdue"
    service.set_deadline(db, row, now + timedelta(hours=5), admin.id, now=now)
    assert row.status == "available" and row.deadline == now + timedelta(hours=5)


def test_submit_gate_and_recording(db):
    from src.checkpoints import service
    admin, course, quiz_course, quiz_lesson, quiz_step, d1, group, s1, s2 = _world(db)
    now = datetime(2026, 9, 11, 9, 0)
    assert service.checkpoint_definition_for_step(db, quiz_step.id).id == d1.id
    with pytest.raises(HTTPException) as e:          # no row → locked
        service.assert_can_submit(db, s1.id, d1, now=now)
    assert e.value.status_code == 403
    row = service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id, now=now)[0]
    assert row.deadline == now + timedelta(hours=24)                       # 24 hours from opening
    assert service.assert_can_submit(db, s1.id, d1, now=now) == [row]
    past_deadline = now + timedelta(hours=service.DEADLINE_HOURS + 1)
    # The deadline is soft: past it the row is flagged overdue but still accepts the attempt.
    assert service.assert_can_submit(db, s1.id, d1, now=past_deadline) == [row]
    assert row.status == "overdue"
    attempt = _attempt(db, s1, quiz_course, quiz_lesson, quiz_step, correct=30)
    done = service.record_submission(db, s1.id, attempt, now=past_deadline + timedelta(minutes=30), commit=False)
    assert done == [row] and row.status == "completed" and row.quiz_attempt_id == attempt.id
    assert (row.correct_answers, row.total_questions, row.percentage) == (30, 45, 66.67)
    assert row.submitted_at == past_deadline + timedelta(minutes=30)
    payload = service.serialize_row(row)
    assert payload["late"] is True and payload["late_minutes"] == 90       # 1h past the deadline + 30 min
    with pytest.raises(HTTPException) as e:          # already completed
        service.assert_can_submit(db, s1.id, d1, now=past_deadline + timedelta(hours=3))
    assert e.value.status_code == 409


def test_on_time_submission_is_not_late(db):
    from src.checkpoints import service
    admin, course, quiz_course, quiz_lesson, quiz_step, d1, group, s1, s2 = _world(db)
    now = datetime(2026, 9, 11, 9, 0)
    row = service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id, now=now)[0]
    attempt = _attempt(db, s1, quiz_course, quiz_lesson, quiz_step, correct=45)
    service.record_submission(db, s1.id, attempt, now=now + timedelta(hours=2), commit=False)
    payload = service.serialize_row(row)
    assert payload["status"] == "completed" and payload["late"] is False and payload["late_minutes"] is None
    assert service.serialize_row(None)["late"] is False


def test_course_access_via_checkpoint(db):
    from src.checkpoints import service
    admin, course, quiz_course, _, _, d1, group, s1, s2 = _world(db)
    assert service.student_has_checkpoint_access_to_course(db, s1.id, quiz_course.id) is False
    service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id)
    assert service.student_has_checkpoint_access_to_course(db, s1.id, quiz_course.id) is True
    assert service.student_has_checkpoint_access_to_course(db, s2.id, quiz_course.id) is False


def test_serialize_for_student_locked_and_open(db):
    from src.checkpoints import service
    admin, course, quiz_course, quiz_lesson, _, d1, group, s1, _ = _world(db)
    v = [u for u in d1.required_units if u.kind == "verbal"]
    complete_lesson_explicit(db, s1, course, db.get(type(quiz_lesson), v[0].lesson_id))
    item = service.serialize_for_student(db, s1.id, group, d1, None)
    assert item["status"] == "locked" and item["number"] == 1 and item["total_questions"] == 45
    assert item["locked_reason"].startswith("Locked — waiting for")
    assert item["quiz"] is None and len(item["covers"]) == 3
    row = service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id)[0]
    item = service.serialize_for_student(db, s1.id, group, d1, row)
    assert item["status"] == "available" and item["quiz"] == {"course_id": quiz_course.id, "lesson_id": quiz_lesson.id}
    assert item["deadline"] is not None and item["locked_reason"] is None


def test_disabling_the_group_revokes_an_open_checkpoint(db):
    """Turning checkpoints off for a group must actually take the checkpoint away, not just hide it."""
    from src.checkpoints import service
    admin, course, quiz_course, quiz_lesson, quiz_step, d1, group, s1, _ = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id)
    assert service.assert_can_submit(db, s1.id, d1)          # sanity: open
    assert service.student_has_checkpoint_access_to_course(db, s1.id, quiz_course.id) is True

    group.checkpoints_enabled = False
    db.flush()
    with pytest.raises(HTTPException) as e:
        service.assert_can_submit(db, s1.id, d1)
    assert e.value.status_code == 403
    assert service.student_has_checkpoint_access_to_course(db, s1.id, quiz_course.id) is False
    assert service.open_checkpoint_lesson_ids_for_student(db, s1.id) == set()


def test_deactivating_the_definition_revokes_an_open_checkpoint(db):
    from src.checkpoints import service
    admin, course, quiz_course, quiz_lesson, quiz_step, d1, group, s1, _ = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id)
    d1.is_active = False
    db.flush()
    with pytest.raises(HTTPException) as e:
        service.assert_can_submit(db, s1.id, d1)
    assert e.value.status_code == 403
    assert service.student_has_checkpoint_access_to_course(db, s1.id, quiz_course.id) is False
    assert service.open_checkpoint_lesson_ids_for_student(db, s1.id) == set()
