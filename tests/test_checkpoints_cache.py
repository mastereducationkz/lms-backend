"""A checkpoint status change must drop the per-user lesson caches.

`get_lesson`, `get_lesson_steps`, `get_module_lessons` and `get_course_lessons` are @cached per
user for up to 300s. Without an explicit invalidation a student keeps seeing a checkpoint's
questions for minutes after it completes or lapses, and does not see a freshly opened one at all.
"""
import pytest

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition,
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


def _world(db):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=2, n_math=1)
    quiz_course, quiz_lesson, quiz_step = make_quiz_lesson(db)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lesson)
    group = make_group(db, enabled=True)
    s = make_user(db)
    enroll(db, s, group, course, admin)
    return admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s


@pytest.fixture
def calls(monkeypatch):
    from src.checkpoints import service
    recorded = []
    monkeypatch.setattr(service, "invalidate", lambda *patterns: recorded.append(patterns) or 0)
    return recorded


EXPECTED = {"courses:lesson:*", "courses:lesson-steps:*",
            "courses:module-lessons:*", "courses:lessons-list:*"}


def test_open_for_students_invalidates_lesson_caches(db, calls):
    from src.checkpoints import service
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    assert calls and set(calls[-1]) == EXPECTED


def test_no_op_open_does_not_invalidate(db, calls):
    from src.checkpoints import service
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    calls.clear()
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    assert calls == []


def test_record_submission_invalidates_lesson_caches(db, calls):
    """As of the blocked-unit rule, completing a checkpoint can unblock the units of a later
    checkpoint that were held back by it, so this must drop the lesson caches."""
    from src.checkpoints import service
    from src.progress.models import QuizAttempt
    admin, course, v, m, quiz_course, quiz_lesson, quiz_step, d1, group, s = _world(db)
    service.open_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    attempt = QuizAttempt(user_id=s.id, step_id=quiz_step.id, course_id=quiz_course.id,
                          lesson_id=quiz_lesson.id, total_questions=2, correct_answers=2,
                          score_percentage=100.0, is_draft=False)
    db.add(attempt); db.flush()
    calls.clear()
    assert service.record_submission(db, s.id, attempt)
    assert calls and set(calls[-1]) == EXPECTED


def test_reopen_invalidates_but_set_deadline_and_sync_do_not(db, calls):
    """reopen_for_students can re-grant a row that was locked/missing, so it stays invalidating.
    set_deadline and sync_student_checkpoints only ever move a row between non-locked statuses
    (or open one that is newly eligible -- sync's own invalidation is covered by
    test_open_for_students_invalidates_lesson_caches's sibling in this file), so neither of the
    first two should fire here."""
    from datetime import timedelta
    from src.checkpoints import service
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    service.reopen_for_students(db, group=group, definition=d1, student_ids=[s.id], actor_id=admin.id)
    assert calls and set(calls[-1]) == EXPECTED
    row = service.get_row(db, s.id, group.id, d1.id)
    calls.clear()
    service.set_deadline(db, row, service.utcnow() + timedelta(hours=5), actor_id=admin.id)
    assert calls == []


def test_sync_student_checkpoints_invalidates_on_auto_open(db, calls):
    from src.checkpoints import service
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    s2 = make_user(db)
    from src.courses.models import GroupStudent
    db.add(GroupStudent(group_id=group.id, student_id=s2.id)); db.flush()
    for l in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, s2, course, l)
    calls.clear()
    assert service.sync_student_checkpoints(db, s2.id)
    assert calls and set(calls[-1]) == EXPECTED


def test_refresh_overdue_invalidates_when_it_flips_rows(db, calls):
    """Overdue no longer blocks (the deadline reversal), so a row actually flipping into overdue
    releases whatever checkpoint-bound units it was holding back and must drop the lesson caches.
    Re-running on rows that are already overdue flips nothing, so it must not fire again."""
    from datetime import timedelta
    from src.checkpoints import service
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    rows = service.open_for_students(db, group=group, definition=d1, student_ids=[s.id],
                                     actor_id=admin.id, now=service.utcnow() - timedelta(hours=service.DEADLINE_HOURS + 24))
    calls.clear()
    assert service.refresh_overdue(rows)
    assert calls and set(calls[-1]) == EXPECTED
    calls.clear()
    assert service.refresh_overdue(rows) == []
    assert calls == []


def test_set_deadline_invalidates_only_when_it_unoverdues_a_row(db, calls):
    """Moving a row from overdue back to available (a future deadline) re-blocks whatever
    checkpoint-bound units it had released, so that transition must invalidate the lesson caches.
    A deadline change with no status transition (still overdue, or already available/reopened)
    must not."""
    from datetime import timedelta
    from src.checkpoints import service
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    rows = service.open_for_students(db, group=group, definition=d1, student_ids=[s.id],
                                     actor_id=admin.id, now=service.utcnow() - timedelta(hours=service.DEADLINE_HOURS + 24))
    assert service.refresh_overdue(rows)
    row = service.get_row(db, s.id, group.id, d1.id)
    assert row.status == "overdue"

    calls.clear()
    service.set_deadline(db, row, service.utcnow() - timedelta(hours=1), actor_id=admin.id)
    assert row.status == "overdue"      # still in the past: no transition
    assert calls == []

    calls.clear()
    service.set_deadline(db, row, service.utcnow() + timedelta(hours=5), actor_id=admin.id)
    assert row.status == "available"    # un-overdue'd: re-blocks
    assert calls and set(calls[-1]) == EXPECTED


def test_invalidation_failure_never_breaks_the_action(db, monkeypatch):
    from src.checkpoints import service
    def boom(*patterns):
        raise RuntimeError("redis is down")
    monkeypatch.setattr(service, "invalidate", boom)
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    assert service.open_for_students(db, group=group, definition=d1, student_ids=[s.id],
                                     actor_id=admin.id)
    assert service.get_row(db, s.id, group.id, d1.id).status == "available"


# --------------------------------------------------------- revocation routes


def test_disabling_a_group_invalidates_lesson_caches(db, calls):
    """update_group_settings can revoke every student's access in the group (checkpoints_enabled
    or a raised start_number), so it must always invalidate -- not just when it opens rows."""
    from src.checkpoints.routes import checkpoints as r
    from src.checkpoints.schemas import GroupSettingsUpdate
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    r.update_group_settings(group.id, GroupSettingsUpdate(enabled=True), current_user=admin, db=db)
    calls.clear()
    r.update_group_settings(group.id, GroupSettingsUpdate(enabled=False), current_user=admin, db=db)
    assert calls and set(calls[-1]) == EXPECTED


def test_deactivating_a_definition_invalidates_lesson_caches(db, calls):
    """update_definition can revoke access via is_active (or repoint quiz_lesson_id / change
    required_units), so it must always invalidate."""
    from src.checkpoints.routes import checkpoints as r
    from src.checkpoints.schemas import DefinitionUpdate
    admin, course, v, m, _, _, _, d1, group, s = _world(db)
    calls.clear()
    r.update_definition(d1.id, DefinitionUpdate(is_active=False), current_user=admin, db=db)
    assert calls and set(calls[-1]) == EXPECTED


def test_lesson_cache_patterns_match_decorated_namespaces():
    from src.checkpoints.service import LESSON_CACHE_PATTERNS
    from src.courses.routes.courses import get_lesson, get_lesson_steps, get_module_lessons, get_course_lessons
    expected = {f"{fn.__cache_namespace__}:*" for fn in (get_lesson, get_lesson_steps, get_module_lessons, get_course_lessons)}
    assert set(LESSON_CACHE_PATTERNS) == expected
