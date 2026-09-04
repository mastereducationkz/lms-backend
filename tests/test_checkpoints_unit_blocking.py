import pytest
from fastapi import HTTPException

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lessons, make_definition,
    complete_lesson_explicit,
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


def _world(db, enabled=True):
    """Two checkpoint blocks plus one unbound unit.

    Block 1 = v[0], v[1], m[0]; Block 2 = v[2], v[3], m[1]; v[4] belongs to no checkpoint.

    v[4] is marked initially-unlocked: v[2]/v[3] are never completed in these tests (that is the
    point — a pending checkpoint holds them back), so the module's own sequential-access chain
    would otherwise also lock v[4] behind them, for a reason that has nothing to do with
    checkpoints and would make it impossible to tell the two apart in the listing-level test.
    """
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=5, n_math=2)
    v[4].is_initially_unlocked = True
    _, quiz_lessons, _ = make_quiz_lessons(db, course, 2)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lessons[0])
    d2 = make_definition(db, course, 2, v[2:4], m[1], quiz_lessons[1])
    group = make_group(db, enabled=enabled)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    db.flush()
    return admin, course, v, m, d1, d2, group, student


def test_no_pending_checkpoint_blocks_nothing(db):
    from src.checkpoints import service
    _, course, v, m, _, _, _, student = _world(db)
    complete_lesson_explicit(db, student, course, v[0])
    assert service.blocked_unit_lesson_ids_for_student(db, student.id) == set()


def test_open_checkpoint_blocks_later_bound_units_only(db):
    from src.checkpoints import service
    _, course, v, m, d1, d2, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)
    assert service.get_row(db, student.id, group.id, d1.id).status == "available"

    blocked = service.blocked_unit_lesson_ids_for_student(db, student.id)
    # block 2's units are bound to a checkpoint and unfinished -> blocked
    assert {v[2].id, v[3].id, m[1].id} <= blocked
    # already-completed units stay open for review
    assert blocked.isdisjoint({v[0].id, v[1].id, m[0].id})
    # a unit bound to no checkpoint is never blocked
    assert v[4].id not in blocked
    # the checkpoint's own quiz lesson is not a "unit"
    assert d1.quiz_lesson_id not in blocked


def test_completing_the_checkpoint_clears_the_block(db):
    from src.checkpoints import service
    from src.schemas.models import QuizAttempt
    from datetime import datetime, timezone
    admin, course, v, m, d1, _, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)
    assert service.blocked_unit_lesson_ids_for_student(db, student.id)

    from src.schemas.models import Step, Lesson, Module
    quiz_step = db.query(Step).filter(Step.lesson_id == d1.quiz_lesson_id).first()
    quiz_course_id = db.query(Module.course_id).join(
        Lesson, Lesson.module_id == Module.id).filter(Lesson.id == d1.quiz_lesson_id).scalar()
    attempt = QuizAttempt(user_id=student.id, step_id=quiz_step.id, course_id=quiz_course_id,
                          lesson_id=d1.quiz_lesson_id, total_questions=45, correct_answers=40,
                          score_percentage=88.89, is_draft=False,
                          completed_at=datetime.now(timezone.utc).replace(tzinfo=None))
    db.add(attempt); db.flush()
    service.record_submission(db, student.id, attempt)
    assert service.blocked_unit_lesson_ids_for_student(db, student.id) == set()


def test_overdue_checkpoint_releases_the_block(db):
    """A lapsed checkpoint no longer holds the student back — once the deadline passes without a
    submission, the student is free to move on to later checkpoint-bound units."""
    from datetime import timedelta
    from src.checkpoints import service
    admin, course, v, m, d1, _, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    opened = service.utcnow() - timedelta(hours=service.DEADLINE_HOURS + 24)
    service.open_for_students(db, group=group, definition=d1, student_ids=[student.id],
                              deadline=opened + timedelta(hours=service.DEADLINE_HOURS),
                              actor_id=admin.id, now=opened)
    row = service.get_row(db, student.id, group.id, d1.id)
    service.refresh_overdue([row])
    db.flush()
    assert row.status == "overdue"
    assert service.blocked_unit_lesson_ids_for_student(db, student.id) == set()


def test_disabled_group_blocks_nothing(db):
    from src.checkpoints import service
    admin, course, v, m, d1, _, group, student = _world(db, enabled=False)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.open_for_students(db, group=group, definition=d1, student_ids=[student.id],
                              actor_id=admin.id)
    group.checkpoints_enabled = False
    db.flush()
    assert service.blocked_unit_lesson_ids_for_student(db, student.id) == set()


def test_listing_and_check_access_agree(db):
    from src.courses.routes.courses import get_course_modules, check_lesson_access
    from src.checkpoints import service
    _, course, v, m, d1, d2, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)

    mods = get_course_modules(course.id, include_lessons=True, student_id=None,
                              current_user=student, db=db)
    rows = {l["id"]: l for mod in mods for l in (mod["lessons"] if isinstance(mod, dict) else mod.lessons)}
    assert rows[v[2].id]["is_accessible"] is False
    assert rows[v[4].id]["is_accessible"] is not False      # unbound unit untouched

    out = check_lesson_access(v[2].id, current_user=student, db=db)
    assert out["accessible"] is False and "Checkpoint 1" in out["reason"]


def test_blocked_unit_reads_are_refused(db):
    from src.courses.routes.courses import get_lesson_steps
    from src.checkpoints import service
    _, course, v, m, d1, _, group, student = _world(db)
    for lesson in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id)
    with pytest.raises(HTTPException) as e:
        get_lesson_steps(v[2].id, include_content=True, current_user=student, db=db)
    assert e.value.status_code == 403 and "Checkpoint 1" in str(e.value.detail)
