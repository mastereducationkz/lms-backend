"""The LMS half of the freeze seam: one task per freeze, whatever happens to it.

The CRM owns the freeze lifecycle and calls across a network that can retry, time out, or run
in two workers at once. So this is an upsert keyed on `freeze_return:{period_id}` with the
uniqueness enforced by the database, not a create endpoint with a caller-side existence check
— the latter is a race dressed up as a guard.

Four things happen to a freeze afterwards and the task must follow all of them without ever
becoming a second task.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.curator.freeze_tasks import (
    RESULT_FREEZE_CANCELLED,
    RESULT_RETURN_CONFIRMED,
    close_freeze_return_task,
    source_key_for,
    upsert_freeze_return_task,
)
# Through the re-export shim: importing `src.curator.models` directly ahead of it trips the
# circular import between the domain models and the schema aggregate.
from src.schemas.models import CuratorTaskInstance, UserInDB

PERIOD = 4242
DUE = datetime(2026, 9, 10)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
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
        session.close()
        trans.rollback()


@pytest.fixture
def curator(db):
    u = UserInDB(email=f"c-{datetime.utcnow().timestamp()}@t.local", name="Куратор",
                 role="curator", hashed_password="x", is_active=True)
    db.add(u); db.flush()
    return u


def _upsert(db, curator, *, due=DUE, title="Возвращение из заморозки", curator_id=None):
    return upsert_freeze_return_task(
        db, freeze_period_id=PERIOD, curator_id=curator_id or curator.id,
        student_id=None, due_date=due, title=title, body="тело",
    )


def _tasks(db):
    return db.query(CuratorTaskInstance).filter(
        CuratorTaskInstance.source_key == source_key_for(PERIOD)
    ).all()


def test_the_first_call_creates_exactly_one_task(db, curator):
    result = _upsert(db, curator)

    assert result["action"] == "created"
    [task] = _tasks(db)
    assert task.curator_id == curator.id
    assert task.due_date == DUE
    assert task.status == "pending"
    assert task.source_key == source_key_for(PERIOD)


def test_replaying_the_same_call_never_makes_a_second_task(db, curator):
    """The property the whole design rests on: a retry after a timeout is free."""
    for _ in range(5):
        _upsert(db, curator)

    assert len(_tasks(db)) == 1


def test_the_database_refuses_a_duplicate_source_key(db, curator):
    """Enforced by the index, not by the caller remembering to look first."""
    from sqlalchemy.exc import IntegrityError

    _upsert(db, curator)
    [existing] = _tasks(db)
    db.add(CuratorTaskInstance(
        template_id=existing.template_id, curator_id=curator.id,
        status="pending", source_key=source_key_for(PERIOD),
    ))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_moving_the_date_reschedules_rather_than_duplicating(db, curator):
    _upsert(db, curator)
    moved = DUE + timedelta(days=7)

    result = _upsert(db, curator, due=moved)

    assert result["action"] == "rescheduled"
    [task] = _tasks(db)
    assert task.due_date == moved


def test_an_unchanged_freeze_reports_no_change(db, curator):
    _upsert(db, curator)
    assert _upsert(db, curator)["action"] == "unchanged"


def test_work_already_started_survives_a_reschedule(db, curator):
    """A curator who has begun has not stopped because the planned date moved."""
    _upsert(db, curator)
    [task] = _tasks(db)
    task.status = "in_progress"
    db.flush()

    _upsert(db, curator, due=DUE + timedelta(days=3))

    [task] = _tasks(db)
    assert task.status == "in_progress"


def test_a_reassigned_curator_follows_the_same_task(db, curator):
    other = UserInDB(email=f"c2-{datetime.utcnow().timestamp()}@t.local", name="Другой",
                     role="curator", hashed_password="x", is_active=True)
    db.add(other); db.flush()
    _upsert(db, curator)

    _upsert(db, curator, curator_id=other.id)

    [task] = _tasks(db)
    assert task.curator_id == other.id, "the work is the same work"


def test_confirming_the_return_completes_the_task(db, curator):
    _upsert(db, curator)

    result = close_freeze_return_task(db, freeze_period_id=PERIOD, outcome="resumed")

    assert result["action"] == "completed"
    [task] = _tasks(db)
    assert task.status == "completed"
    assert task.result_text == RESULT_RETURN_CONFIRMED
    assert task.completed_at is not None


def test_cancelling_the_freeze_supersedes_the_task_without_deleting_it(db, curator):
    _upsert(db, curator)

    close_freeze_return_task(db, freeze_period_id=PERIOD, outcome="cancelled")

    [task] = _tasks(db)
    assert task.status == "cancelled"
    assert task.result_text == RESULT_FREEZE_CANCELLED
    assert len(_tasks(db)) == 1, "history preserved, not removed"


def test_closing_twice_is_a_no_op(db, curator):
    _upsert(db, curator)
    close_freeze_return_task(db, freeze_period_id=PERIOD, outcome="resumed")

    again = close_freeze_return_task(db, freeze_period_id=PERIOD, outcome="resumed")

    assert again["action"] == "unchanged"


def test_a_finished_task_is_not_reopened_by_a_later_date_change(db, curator):
    _upsert(db, curator)
    close_freeze_return_task(db, freeze_period_id=PERIOD, outcome="resumed")

    result = _upsert(db, curator, due=DUE + timedelta(days=30))

    assert result["action"] == "unchanged"
    [task] = _tasks(db)
    assert task.status == "completed"


def test_closing_a_freeze_that_never_had_a_task_is_harmless(db):
    assert close_freeze_return_task(
        db, freeze_period_id=999999, outcome="resumed"
    )["action"] == "absent"


def test_the_task_never_resumes_the_student(db, curator):
    """A planned date arriving is not a fact about the student; a curator saying so is.

    That is the whole reason the freeze produces a task instead of a state transition, and
    nothing in this module may write to the account.
    """
    import inspect

    import src.curator.freeze_tasks as module

    source = inspect.getsource(module)
    assert "study_status" not in source
    assert "StudentAccount" not in source
