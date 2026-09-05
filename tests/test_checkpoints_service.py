from datetime import datetime, timedelta

import pytest

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition,
    complete_lesson_explicit, complete_lesson_via_steps,
)


@pytest.fixture
def db():
    # join_transaction_mode="create_savepoint" (SQLAlchemy 2.0) instead of the older
    # begin_nested()+after_transaction_end-listener recipe: that recipe rebuilds its savepoint by
    # reacting to the transaction the app's own db.commit() just tore down, so an app-level
    # db.rollback() straight after a commit unwinds past it and takes committed rows with it.
    # create_savepoint keeps every app-level commit/rollback nested one level down, inside a
    # connection-level transaction this fixture always rolls back. See tests/onboarding_fixtures.py.
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
    assert row.opened_at == now and row.deadline == now + timedelta(hours=service.DEADLINE_HOURS)
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


def test_archived_group_revokes_checkpoint_access(db):
    """`enabled_groups_for_student` already skips archived groups, but the three access helpers
    only looked at `checkpoints_enabled` — so a row in a group that had been archived still let
    the student open the hidden quiz course and submit."""
    from fastapi import HTTPException
    from src.checkpoints import service
    admin, course, v, m, d1, d2, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    quiz_course_id = service.quiz_ref(db, d1)["course_id"]
    assert service.assert_can_submit(db, s.id, d1)
    assert service.student_has_checkpoint_access_to_course(db, s.id, quiz_course_id) is True
    assert d1.quiz_lesson_id in service.open_checkpoint_lesson_ids_for_student(db, s.id)

    group.is_active = False
    db.flush()
    with pytest.raises(HTTPException) as e:
        service.assert_can_submit(db, s.id, d1)
    assert e.value.status_code == 403
    assert service.student_has_checkpoint_access_to_course(db, s.id, quiz_course_id) is False
    assert d1.quiz_lesson_id not in service.open_checkpoint_lesson_ids_for_student(db, s.id)


def test_several_checkpoints_opening_at_once_get_staggered_deadlines(db):
    """A student who already finished blocks 1 and 2 when the group is switched on gets both
    checkpoints, due a day apart in checkpoint order — not both due in 24 hours."""
    from src.checkpoints import service
    _, course, v, m, d1, d2, group, s = _world(db)
    for l in (v[0], v[1], m[0], v[2], v[3], m[1]):
        complete_lesson_explicit(db, s, course, l)
    now = datetime(2026, 9, 10, 12, 0)
    opened = service.sync_student_checkpoints(db, s.id, now=now, commit=False)
    assert [r.checkpoint_number for r in opened] == [1, 2]
    assert opened[0].deadline == now + timedelta(hours=24)
    assert opened[1].deadline == now + timedelta(hours=48)
    assert service.sync_student_checkpoints(db, s.id, now=now + timedelta(hours=1), commit=False) == []


def test_a_checkpoint_passed_in_another_group_carries_over(db):
    """Moving a student to another checkpoints-enabled group must not make them retake what they
    already passed: the new group's row is created completed with the same result."""
    from src.checkpoints import service
    from tests.checkpoint_fixtures import complete_checkpoint
    admin, course, v, m, d1, d2, group, s = _world(db)
    for l in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, s, course, l)
    service.sync_student_checkpoints(db, s.id, commit=False)
    complete_checkpoint(db, s, d1, correct=40, total=45)
    old_row = service.get_row(db, s.id, group.id, d1.id)
    assert old_row.status == "completed"

    new_group = make_group(db, enabled=True, name="cp-grp-2")
    enroll(db, s, new_group, course, admin)
    opened = service.sync_student_checkpoints(db, s.id, commit=False)
    assert opened == []                                   # nothing newly opened for the student
    carried = service.get_row(db, s.id, new_group.id, d1.id)
    assert carried is not None and carried.status == "completed" and carried.opened_by == "transfer"
    assert (carried.correct_answers, carried.total_questions, carried.percentage) == (40, 45, old_row.percentage)
    assert carried.quiz_attempt_id == old_row.quiz_attempt_id and carried.submitted_at == old_row.submitted_at
    assert service.blocked_unit_lesson_ids_for_student(db, s.id) == set()    # cleared in the new group too


def test_unit_options_follow_the_modules(db):
    from src.checkpoints import service
    from tests.checkpoint_fixtures import make_quiz_lessons
    from src.courses.models import Lesson, Module
    _, course, v, m, d1, d2, group, s = _world(db)
    make_quiz_lessons(db, course, 1)                                          # a checkpoint lesson: excluded
    verbal_module = db.query(Module).filter_by(course_id=course.id, title="Verbal").one()
    db.add(Lesson(title="Unit 0. Getting Started", module_id=verbal_module.id, order_index=0)); db.flush()
    options = service.unit_options(db, course.id)
    assert [o["lesson_id"] for o in options] == [l.id for l in v] + [l.id for l in m]
    assert {o["kind"] for o in options if o["lesson_id"] in {l.id for l in v}} == {"verbal"}
    assert {o["kind"] for o in options if o["lesson_id"] in {l.id for l in m}} == {"math"}
    assert all(o["module"] in ("Verbal", "Math") for o in options)
