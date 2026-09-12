"""A group's Telegram chat is told when a new assignment is published, with a link to open
it (owner, 2026-09-12).

Queued right after ``create_assignment``'s own commit — see
``src.assignments.routes.assignments`` — and drained by ``send_due_notices``, the same shape
as the lesson-change-notice job.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink, TelegramHomeworkNotice
from src.assignments.routes.assignments import create_assignment
from src.assignments.schemas import AssignmentCreateSchema
from src.schemas.models import Group, UserInDB
from src.services import telegram_homework_notices as notices
from src.utils.auth_utils import hash_password


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


@pytest.fixture(autouse=True)
def switched_on(monkeypatch):
    monkeypatch.setenv("ENABLE_TELEGRAM_HOMEWORK_NOTICES", "1")


_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _teacher(db) -> UserInDB:
    u = UserInDB(email=f"hw-teacher{_uniq()}@test.local", name="Teacher", role="teacher",
                hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _group(db, teacher, name=None) -> Group:
    g = Group(name=name or f"group{_uniq()}", teacher_id=teacher.id, is_active=True)
    db.add(g); db.flush(); return g


def _link(db, group, support_group_id=None):
    db.add(TelegramGroupLink(lms_group_id=group.id, support_group_id=support_group_id or _uniq(),
                             chat_title=group.name))
    db.flush()


def _create(db, teacher, group, **overrides):
    data = AssignmentCreateSchema(
        title=overrides.pop("title", "Reading practice"),
        assignment_type="free_text",
        content={"question": "Summarise chapter 3"},
        group_id=group.id,
        **overrides,
    )
    return create_assignment(assignment_data=data, current_user=teacher, db=db)


# ── queued exactly when an assignment is actually published ──────────────────────────────────


def test_creating_an_assignment_queues_one_notice_per_linked_group(db):
    teacher = _teacher(db)
    group = _group(db, teacher)
    _link(db, group, support_group_id=701)

    result = _create(db, teacher, group)

    row = db.query(TelegramHomeworkNotice).filter_by(lms_group_id=group.id).one()
    assert row.assignment_id == result.id
    assert row.support_group_id == 701
    assert row.status == "pending" and row.attempts == 0


def test_no_linked_chat_queues_nothing(db):
    teacher = _teacher(db)
    group = _group(db, teacher)                      # no TelegramGroupLink

    _create(db, teacher, group)

    assert db.query(TelegramHomeworkNotice).count() == 0


def test_switched_off_queues_nothing(db, monkeypatch):
    monkeypatch.delenv("ENABLE_TELEGRAM_HOMEWORK_NOTICES", raising=False)
    teacher = _teacher(db)
    group = _group(db, teacher)
    _link(db, group)

    _create(db, teacher, group)

    assert db.query(TelegramHomeworkNotice).count() == 0


def test_a_lesson_only_assignment_with_no_group_queues_nothing(db):
    teacher = _teacher(db)
    group = _group(db, teacher)
    _link(db, group)

    data = AssignmentCreateSchema(title="No group", assignment_type="free_text",
                                  content={"question": "q"})
    create_assignment(assignment_data=data, current_user=teacher, db=db)

    assert db.query(TelegramHomeworkNotice).count() == 0


def test_calling_queue_for_assignment_twice_does_not_double_queue(db):
    teacher = _teacher(db)
    group = _group(db, teacher)
    _link(db, group, support_group_id=702)
    result = _create(db, teacher, group)
    from src.schemas.models import Assignment
    assignment = db.get(Assignment, result.id)

    again = notices.queue_for_assignment(db, assignment, group)

    assert again == 0
    assert db.query(TelegramHomeworkNotice).filter_by(assignment_id=assignment.id).count() == 1


def test_notice_text_builds_a_real_hyperlink_and_escapes_the_rest():
    text = notices.notice_text("Chapter <3> & review", "AI & ML group", None,
                               "https://lms.mastereducation.kz/homework/42")
    assert '<a href="https://lms.mastereducation.kz/homework/42">Открыть задание</a>' in text
    assert "Chapter &lt;3&gt; &amp; review" in text
    assert "AI &amp; ML group" in text


def test_notice_text_includes_the_due_date_when_set():
    due = datetime(2026, 9, 20, 15, 0)
    text = notices.notice_text("Essay", "group", due, "https://lms.mastereducation.kz/homework/1")
    assert "Срок:" in text


# ── send_due_notices ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def queued(db):
    teacher = _teacher(db)
    group = _group(db, teacher)
    _link(db, group, support_group_id=801)
    result = _create(db, teacher, group)
    return {"db": db, "assignment_id": result.id, "group": group}


def test_send_due_notices_posts_to_support_and_marks_it_sent(queued, monkeypatch):
    db = queued["db"]
    calls = []

    def fake_call(method, path, **kwargs):
        calls.append((method, path, kwargs["json_body"]))
        return {"telegram_message_id": 999}
    monkeypatch.setattr(notices.support_client, "call", fake_call)

    summary = notices.send_due_notices(db)

    assert summary["sent"] == 1
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/telegram/messages")
    assert body["telegram_group_id"] == 801
    assert body["idempotency_key"] == f"homework:{queued['assignment_id']}:{queued['group'].id}"
    assert "Открыть задание" in body["text"]
    row = db.query(TelegramHomeworkNotice).one()
    assert row.status == "sent" and row.telegram_message_id == 999 and row.sent_at is not None
    assert notices.send_due_notices(db) == {"sent": 0, "failed": 0, "skipped": 0}, "sent once, not again"


def test_a_chat_support_no_longer_knows_is_skipped_not_retried(queued, monkeypatch):
    db = queued["db"]

    def fail(method, path, **kwargs):
        raise HTTPException(status_code=404, detail="chat not found")
    monkeypatch.setattr(notices.support_client, "call", fail)

    summary = notices.send_due_notices(db)

    assert summary["skipped"] == 1
    row = db.query(TelegramHomeworkNotice).one()
    assert row.status == "skipped"


def test_a_passing_failure_is_retried_up_to_the_limit(queued, monkeypatch):
    db = queued["db"]

    def fail(method, path, **kwargs):
        raise HTTPException(status_code=502, detail="Support unreachable")
    monkeypatch.setattr(notices.support_client, "call", fail)

    for _ in range(notices.MAX_ATTEMPTS):
        notices.send_due_notices(db)
    row = db.query(TelegramHomeworkNotice).one()
    assert row.status == "failed" and row.attempts == notices.MAX_ATTEMPTS
    assert notices.send_due_notices(db) == {"sent": 0, "failed": 0, "skipped": 0}, "gave up for good"


def test_switched_off_sends_nothing(queued, monkeypatch):
    monkeypatch.delenv("ENABLE_TELEGRAM_HOMEWORK_NOTICES", raising=False)
    db = queued["db"]
    monkeypatch.setattr(notices.support_client, "call",
                        lambda *a, **k: pytest.fail("must not call Support while switched off"))

    assert notices.send_due_notices(db) == {"sent": 0, "failed": 0, "skipped": 0}
