from datetime import datetime, timedelta

import pytest

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition,
    complete_lesson_explicit, complete_lesson_via_steps,
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


def _world(db, enabled=True, start_number=1):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=4, n_math=2)
    _, quiz_lesson, _ = make_quiz_lesson(db)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lesson)
    d2 = make_definition(db, course, 2, v[2:4], m[1], quiz_lesson)
    group = make_group(db, enabled=enabled, start_number=start_number)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    return admin, course, v, m, d1, d2, group, student


def test_opens_only_when_all_three_units_done_any_order(db):
    from src.checkpoints import service
    _, course, v, m, d1, d2, group, s = _world(db)
    now = datetime(2026, 9, 10, 18, 0)
    complete_lesson_explicit(db, s, course, v[0])
    complete_lesson_via_steps(db, s, course, m[0])          # math done BEFORE second verbal
    assert service.sync_student_checkpoints(db, s.id, now=now, commit=False) == []
    complete_lesson_explicit(db, s, course, v[1])
    opened = service.sync_student_checkpoints(db, s.id, now=now, commit=False)
    assert [r.checkpoint_number for r in opened] == [1]
    row = opened[0]
    assert row.status == "available" and row.opened_by == "auto"
    assert row.opened_at == now and row.deadline == now + timedelta(hours=24)
    assert row.required_unit_ids == [v[0].id, v[1].id, m[0].id]
    assert row.group_id == group.id and row.checkpoint_id == d1.id
    # idempotent
    assert service.sync_student_checkpoints(db, s.id, now=now, commit=False) == []


def test_disabled_group_never_opens(db):
    from src.checkpoints import service
    _, course, v, m, d1, _, group, s = _world(db, enabled=False)
    for l in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, s, course, l)
    assert service.sync_student_checkpoints(db, s.id, commit=False) == []
    assert service.get_row(db, s.id, group.id, d1.id) is None


def test_inactive_definition_and_start_number_skip(db):
    from src.checkpoints import service
    _, course, v, m, d1, d2, group, s = _world(db, start_number=2)
    for l in (v[0], v[1], m[0], v[2], v[3], m[1]):
        complete_lesson_explicit(db, s, course, l)
    d2.is_active = False; db.flush()
    assert service.sync_student_checkpoints(db, s.id, commit=False) == []   # 1 < start, 2 inactive
    d2.is_active = True; db.flush()
    opened = service.sync_student_checkpoints(db, s.id, commit=False)
    assert [r.checkpoint_number for r in opened] == [2]


def test_unit_progress_and_locked_reason(db):
    from src.checkpoints import service
    _, course, v, m, d1, _, group, s = _world(db)
    complete_lesson_explicit(db, s, course, v[0])
    units = service.unit_progress(db, s.id, d1)
    assert [(u["lesson_id"], u["kind"], u["completed"]) for u in units] == [
        (v[0].id, "verbal", True), (v[1].id, "verbal", False), (m[0].id, "math", False)]
    assert service.locked_reason(units) == "Locked — waiting for Unit 2: Verbal, Unit 1: Math"
    for l in (v[1], m[0]):
        complete_lesson_explicit(db, s, course, l)
    assert service.locked_reason(service.unit_progress(db, s.id, d1)) is None


def test_refresh_overdue_flips_only_open_rows_past_deadline(db):
    from src.checkpoints import service
    from src.checkpoints.models import StudentCheckpoint
    _, course, v, m, d1, d2, group, s = _world(db)
    now = datetime(2026, 9, 12, 12, 0)
    a = StudentCheckpoint(student_id=s.id, group_id=group.id, checkpoint_id=d1.id, checkpoint_number=1,
                          status="available", opened_at=now - timedelta(hours=30), deadline=now - timedelta(hours=6))
    b = StudentCheckpoint(student_id=s.id, group_id=group.id, checkpoint_id=d2.id, checkpoint_number=2,
                          status="completed", opened_at=now - timedelta(hours=30), deadline=now - timedelta(hours=6),
                          submitted_at=now - timedelta(hours=10))
    db.add_all([a, b]); db.flush()
    flipped = service.refresh_overdue([a, b], now)
    assert flipped == [a] and a.status == "overdue" and b.status == "completed"
