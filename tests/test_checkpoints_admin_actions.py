from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.schemas.models import QuizAttempt
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
    assert all(r.status == "available" and r.opened_by == "admin" and r.deadline == now + timedelta(hours=24) for r in rows)
    # open again is a no-op for already-open rows
    assert service.open_for_students(db, group=group, definition=d1, actor_id=admin.id, now=now) == []
    later = now + timedelta(days=3)
    re = service.reopen_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id, now=later)
    assert len(re) == 1 and re[0].student_id == s1.id and re[0].status == "reopened"
    assert re[0].reopen_count == 1 and re[0].deadline == later + timedelta(hours=24)
    assert service.get_row(db, s2.id, group.id, d1.id).status == "available"


def test_set_deadline_revives_overdue(db):
    from src.checkpoints import service
    admin, _, _, _, _, d1, group, s1, _ = _world(db)
    now = datetime(2026, 9, 12, 12, 0)
    row = service.open_for_students(db, group=group, definition=d1, student_ids=[s1.id], actor_id=admin.id,
                                    now=now - timedelta(days=2))[0]
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
    assert service.assert_can_submit(db, s1.id, d1, now=now) == [row]
    with pytest.raises(HTTPException) as e:          # deadline passed
        service.assert_can_submit(db, s1.id, d1, now=now + timedelta(hours=25))
    assert e.value.status_code == 409
    attempt = _attempt(db, s1, quiz_course, quiz_lesson, quiz_step, correct=30)
    done = service.record_submission(db, s1.id, attempt, now=now + timedelta(hours=2), commit=False)
    assert done == [row] and row.status == "completed" and row.quiz_attempt_id == attempt.id
    assert (row.correct_answers, row.total_questions, row.percentage) == (30, 45, 66.67)
    assert row.submitted_at == now + timedelta(hours=2)
    with pytest.raises(HTTPException) as e:          # already completed
        service.assert_can_submit(db, s1.id, d1, now=now + timedelta(hours=3))
    assert e.value.status_code == 409


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
