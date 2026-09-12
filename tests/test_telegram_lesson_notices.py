"""A group's Telegram chat is told when an approved request moves, cancels, or hands off its
lesson (owner, 2026-09-12).

Queued inside the same approval that changes the event — see
``src.lesson_requests.helpers`` — and drained by ``send_due_notices``, the same shape as the
lesson-invitation job. The fixtures reuse ``test_lesson_request_cancel_resolution``'s course
builder and approval helper: the trigger for a notice is exactly the trigger for the event
change itself.
"""
import asyncio
from datetime import date, datetime, time, timedelta

import pytest
from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink, TelegramLessonChangeNotice
from src.lesson_requests import routes as lr_routes
from src.lesson_requests.schemas import CreateLessonRequestSchema, ResolveLessonRequestSchema
from src.lesson_requests.services import create_lesson_request_record
from src.schemas.models import Event, EventGroup, Group, LessonRequest, UserInDB
from src.services import telegram_lesson_notices as notices
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


KZ = timedelta(hours=5)
SLOT = time(19, 0)
MON, THU = 0, 3
SCHEDULE_ITEMS = [{"day_of_week": MON, "time_of_day": "19:00"}, {"day_of_week": THU, "time_of_day": "19:00"}]

_seq = 0


def _uniq() -> int:
    global _seq
    _seq += 1
    return _seq


def _this_monday() -> date:
    today_local = (datetime.utcnow() + KZ).date()
    return today_local - timedelta(days=today_local.weekday())


def _slot_utc(local_day: date) -> datetime:
    return datetime.combine(local_day, SLOT) - KZ


def _user(db, role, name=None):
    u = UserInDB(email=f"notice-{role}{_uniq()}@test.local", name=name or role.title(), role=role,
               hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _lesson(db, group, *, start, minutes=60, title=None, teacher_id=None):
    ev = Event(title=title or f"{group.name}: Lesson", event_type="class",
              start_datetime=start, end_datetime=start + timedelta(minutes=minutes),
              is_active=True, is_online=True, location="Online",
              teacher_id=teacher_id if teacher_id is not None else group.teacher_id,
              created_by=group.teacher_id)
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group.id)); db.flush()
    return ev


def _course(db, teacher, *, offsets=(-14, -11, -7, -4, 7, 10), name=None):
    monday = _this_monday()
    group = Group(name=name or f"Notice G{_uniq()}", is_active=True, is_over=False, teacher_id=teacher.id,
                 program_type="sat", schedule_config={
                     "start_date": (monday + timedelta(days=min(offsets))).isoformat(),
                     "weeks_count": 5, "lessons_count": len(offsets), "schedule_items": SCHEDULE_ITEMS})
    db.add(group); db.flush()
    events = [_lesson(db, group, start=_slot_utc(monday + timedelta(days=off)),
                      title=f"{group.name}: Lesson {i}") for i, off in enumerate(offsets, start=1)]
    return group, events


def _link(db, group, support_group_id=None):
    db.add(TelegramGroupLink(lms_group_id=group.id, support_group_id=support_group_id or _uniq(),
                             chat_title=group.name))
    db.flush()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _approve(db, approver, lr, **kwargs):
    data = ResolveLessonRequestSchema(**kwargs)
    return _run(lr_routes.approve_lesson_request(request_id=lr.id, data=data, db=db, current_user=approver))


def _reschedule_request(db, teacher, group, event, new_dt) -> LessonRequest:
    return create_lesson_request_record(
        db, teacher, CreateLessonRequestSchema(
            request_type="reschedule", event_id=event.id, group_id=group.id,
            original_datetime=event.start_datetime, new_datetime=new_dt))


def _cancel_request(db, teacher, group, event) -> LessonRequest:
    return create_lesson_request_record(
        db, teacher, CreateLessonRequestSchema(
            request_type="cancel", event_id=event.id, group_id=group.id,
            original_datetime=event.start_datetime))


def _substitution_request(db, teacher, group, event, substitute) -> LessonRequest:
    return create_lesson_request_record(
        db, teacher, CreateLessonRequestSchema(
            request_type="substitution", event_id=event.id, group_id=group.id,
            original_datetime=event.start_datetime, substitute_teacher_ids=[substitute.id]))


@pytest.fixture(autouse=True)
def switched_on(monkeypatch):
    monkeypatch.setenv("ENABLE_TELEGRAM_LESSON_CHANGE_NOTICES", "1")


# ── queued exactly when the event actually changes ────────────────────────────────────────


def test_a_reschedule_queues_one_notice_with_the_old_and_new_time(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    _link(db, group, support_group_id=501)
    target = events[4]
    old_start = target.start_datetime
    new_dt = old_start + timedelta(days=1)
    lr = _reschedule_request(db, teacher, group, target, new_dt)

    _approve(db, admin, lr)

    row = db.query(TelegramLessonChangeNotice).filter_by(lesson_request_id=lr.id).one()
    assert row.change_type == notices.RESCHEDULED
    assert row.lms_group_id == group.id and row.support_group_id == 501
    assert row.old_start_datetime == old_start and row.new_start_datetime == new_dt
    assert row.status == "pending" and row.attempts == 0


def test_a_cancel_queues_a_notice_with_the_lessons_own_time(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    _link(db, group, support_group_id=502)
    target = events[4]
    lr = _cancel_request(db, teacher, group, target)

    _approve(db, admin, lr, cancel_resolution="cancel_only")

    row = db.query(TelegramLessonChangeNotice).filter_by(lesson_request_id=lr.id).one()
    assert row.change_type == notices.CANCELLED
    assert row.old_start_datetime == target.start_datetime
    assert row.new_start_datetime is None


def test_a_substitution_queues_a_notice_naming_the_new_teacher(db):
    teacher, admin, substitute = _user(db, "teacher"), _user(db, "admin"), _user(db, "teacher")
    group, events = _course(db, teacher)
    _link(db, group, support_group_id=503)
    target = events[4]
    lr = _substitution_request(db, teacher, group, target, substitute)

    _approve(db, admin, lr, confirmed_teacher_id=substitute.id)

    row = db.query(TelegramLessonChangeNotice).filter_by(lesson_request_id=lr.id).one()
    assert row.change_type == notices.SUBSTITUTED
    assert row.old_teacher_id == teacher.id and row.new_teacher_id == substitute.id


def test_no_linked_chat_queues_nothing(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)                      # no TelegramGroupLink
    target = events[4]
    lr = _cancel_request(db, teacher, group, target)

    _approve(db, admin, lr, cancel_resolution="cancel_only")

    assert db.query(TelegramLessonChangeNotice).count() == 0


def test_switched_off_queues_nothing(db, monkeypatch):
    monkeypatch.delenv("ENABLE_TELEGRAM_LESSON_CHANGE_NOTICES", raising=False)
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    _link(db, group)
    lr = _cancel_request(db, teacher, group, events[4])

    _approve(db, admin, lr, cancel_resolution="cancel_only")

    assert db.query(TelegramLessonChangeNotice).count() == 0


def test_a_shared_lesson_notifies_every_linked_group(db):
    """One Event, two groups: moving it moves both classes, so both chats hear about it."""
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group_a, events = _course(db, teacher, name="Shared A")
    group_b = Group(name="Shared B", is_active=True, is_over=False, teacher_id=teacher.id, program_type="sat")
    db.add(group_b); db.flush()
    target = events[4]
    db.add(EventGroup(event_id=target.id, group_id=group_b.id)); db.flush()
    _link(db, group_a, support_group_id=601)
    _link(db, group_b, support_group_id=602)
    lr = _reschedule_request(db, teacher, group_a, target, target.start_datetime + timedelta(days=1))

    _approve(db, admin, lr)

    rows = db.query(TelegramLessonChangeNotice).filter_by(lesson_request_id=lr.id).all()
    assert {r.lms_group_id for r in rows} == {group_a.id, group_b.id}
    assert {r.support_group_id for r in rows} == {601, 602}


def test_an_approval_that_changes_nothing_queues_nothing(db):
    """Re-approving a cancel already applied elsewhere (see the cancel-resolution suite) must
    not say a second time that a lesson only just got cancelled."""
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    _link(db, group)
    target = events[4]
    lr = _cancel_request(db, teacher, group, target)
    target.is_active = False
    db.flush()

    _approve(db, admin, lr, cancel_resolution="cancel_only")

    assert db.query(TelegramLessonChangeNotice).count() == 0


def test_a_second_approval_for_the_same_group_does_not_double_queue(db):
    """The unique constraint, exercised directly: queuing twice for one (request, group) queues
    once — the same guard the invitation job leans on."""
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    _link(db, group, support_group_id=701)
    target = events[4]
    lr = _cancel_request(db, teacher, group, target)
    _approve(db, admin, lr, cancel_resolution="cancel_only")
    assert db.query(TelegramLessonChangeNotice).count() == 1

    queued_again = notices.queue_for_request(db, lr, target, notices.CANCELLED, old_start=target.start_datetime)

    assert queued_again == 0
    assert db.query(TelegramLessonChangeNotice).count() == 1


# ── the text ───────────────────────────────────────────────────────────────────────────────


def test_notice_text_says_what_changed_and_never_a_reason():
    start = datetime(2026, 9, 14, 14, 0)          # 19:00 Almaty
    new_start = datetime(2026, 9, 16, 14, 0)
    rescheduled = notices.notice_text(notices.RESCHEDULED, "July 8 SAT, урок 5",
                                      old_start=start, new_start=new_start, meeting_url="https://meet.google.com/x")
    assert "перенесён" in rescheduled and "Было:" in rescheduled and "Стало:" in rescheduled
    assert "meet.google.com" in rescheduled

    cancelled = notices.notice_text(notices.CANCELLED, "July 8 SAT, урок 5", old_start=start)
    assert "отменён" in cancelled

    substituted = notices.notice_text(notices.SUBSTITUTED, "July 8 SAT, урок 5",
                                      old_start=start, new_teacher_name="Гульзада Сапарова")
    assert "другой преподаватель" in substituted and "Гульзада Сапарова" in substituted
    # A reason is a teacher's private business with the approver, never the room's.
    for text in (rescheduled, cancelled, substituted):
        assert "reason" not in text.lower() and "причин" not in text.lower()


# ── sending ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def queued(db):
    teacher, admin = _user(db, "teacher"), _user(db, "admin")
    group, events = _course(db, teacher)
    _link(db, group, support_group_id=801)
    target = events[4]
    lr = _cancel_request(db, teacher, group, target)
    _approve(db, admin, lr, cancel_resolution="cancel_only")
    return {"db": db, "lr": lr, "group": group}


def test_send_due_notices_posts_to_support_and_marks_it_sent(queued, monkeypatch):
    db = queued["db"]
    calls = []

    def fake_call(method, path, **kwargs):
        calls.append((method, path, kwargs["json_body"]))
        return {"telegram_message_id": 999}
    monkeypatch.setattr(notices.support_client, "call", fake_call)

    summary = notices.send_due_notices(db)

    assert summary["sent"] == 1
    assert len(calls) == 1
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/telegram/messages")
    assert body["telegram_group_id"] == 801
    assert body["idempotency_key"] == f"lesson-change:{queued['lr'].id}:{queued['group'].id}"
    row = db.query(TelegramLessonChangeNotice).one()
    assert row.status == "sent" and row.telegram_message_id == 999 and row.sent_at is not None
    assert notices.send_due_notices(db) == {"sent": 0, "failed": 0, "skipped": 0}, "sent once, not again"


def test_a_chat_support_no_longer_knows_is_skipped_not_retried(queued, monkeypatch):
    db = queued["db"]

    def fail(method, path, **kwargs):
        raise HTTPException(status_code=404, detail="chat not found")
    monkeypatch.setattr(notices.support_client, "call", fail)

    summary = notices.send_due_notices(db)

    assert summary["skipped"] == 1
    row = db.query(TelegramLessonChangeNotice).one()
    assert row.status == "skipped"
    assert notices.send_due_notices(db) == {"sent": 0, "failed": 0, "skipped": 0}, "skipped is final"


def test_a_passing_failure_is_retried_up_to_the_limit(queued, monkeypatch):
    db = queued["db"]

    def fail(method, path, **kwargs):
        raise HTTPException(status_code=502, detail="Support unreachable")
    monkeypatch.setattr(notices.support_client, "call", fail)

    for _ in range(notices.MAX_ATTEMPTS):
        notices.send_due_notices(db)
    row = db.query(TelegramLessonChangeNotice).one()
    assert row.status == "failed" and row.attempts == notices.MAX_ATTEMPTS
    assert notices.send_due_notices(db) == {"sent": 0, "failed": 0, "skipped": 0}, "gave up for good"


def test_switched_off_sends_nothing(queued, monkeypatch):
    monkeypatch.delenv("ENABLE_TELEGRAM_LESSON_CHANGE_NOTICES", raising=False)
    db = queued["db"]
    monkeypatch.setattr(notices.support_client, "call",
                        lambda *a, **k: pytest.fail("must not call Support while switched off"))

    assert notices.send_due_notices(db) == {"sent": 0, "failed": 0, "skipped": 0}
