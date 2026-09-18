"""The morning post to «Штрафы учителя».

What a chat says is harder to take back than what a page shows, so this file is mostly about
what the post leaves out: minutes that were worked off, fines a head teacher has already
waived, and days when nothing happened. A post that cries wolf stops being read, and then the
register loses the only audience that acts on it.

The one thing it must never leave out is a lesson that never happened — even before anybody has
put a price on it.
"""
from datetime import date, datetime, timedelta

import pytest

from src.discipline import digest, service
from src.discipline.models import DisciplineDigestSend
from src.discipline.rules import period_containing

DAY = date(2026, 9, 17)
# 09:00 Almaty on the 18th is 04:00 UTC — the morning after the lessons being reported.
MORNING = datetime(2026, 9, 18, 4, 0)


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
        session.close()
        trans.rollback()
        connection.close()


def _person(db, role, name):
    from src.schemas.models import UserInDB
    from src.utils.auth_utils import hash_password
    user = UserInDB(email=f"dig-{datetime.now().timestamp():.6f}-{role}@test.local", name=name,
                    role=role, hashed_password=hash_password("x"), is_active=True)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def world(db, monkeypatch):
    """One teacher, one group, and a lesson at 13:00–14:00 Almaty on 17.09."""
    from src.schemas.models import Event, EventGroup, Group, GroupStudent
    teacher = _person(db, "teacher", "Қайратқызы Дина")
    head = _person(db, "head_teacher", "Head Teacher")
    group = Group(name="NUET Sep 2", is_active=True, is_over=False, program_type="NUET",
                  teacher_id=teacher.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=_person(db, "student", "Ученик").id))

    timings = {}
    lessons = []

    def lesson(*, start=datetime(2026, 9, 17, 8, 0), minutes=60, state="ready",
               first_join=None, last_leave=None):
        """`start` is naive UTC; 08:00 UTC is 13:00 Almaty."""
        event = Event(title="NUET, урок", event_type="class", start_datetime=start,
                      end_datetime=start + timedelta(minutes=minutes), created_by=teacher.id,
                      teacher_id=teacher.id, is_active=True)
        db.add(event)
        db.flush()
        db.add(EventGroup(event_id=event.id, group_id=group.id))
        db.flush()
        timings[event.id] = (state, first_join, last_leave, 8, 9)
        lessons.append(event)
        return event

    monkeypatch.setattr(service, "_timings", lambda db_, events, now: {
        e.id: timings.get(e.id, ("no_room", None, None, 0, 0)) for e in events})
    monkeypatch.setenv("ENABLE_DISCIPLINE_DIGEST", "true")
    monkeypatch.setenv("DISCIPLINE_NOTICES_SUPPORT_GROUP_ID", "77")
    monkeypatch.setenv("DISCIPLINE_NOTICES_TOPIC_ID", "9347")
    return {"teacher": teacher, "head": head, "group": group, "lesson": lesson}


@pytest.fixture
def posted(monkeypatch):
    """Capture what would have gone to Telegram."""
    sent = []

    def fake_post(group_id, text, key, *, silent, topic_id=None, **kw):
        sent.append({"group_id": group_id, "text": text, "key": key, "topic_id": topic_id})
        return {"status": "sent", "telegram_message_id": 500 + len(sent)}

    monkeypatch.setattr(digest.outbox, "post", fake_post)
    return sent


def _late(world, minutes, *, made_up=False):
    start = datetime(2026, 9, 17, 8, 0)
    end = start + timedelta(minutes=60)
    leave = end + timedelta(minutes=minutes) if made_up else end
    return world["lesson"](start=start, first_join=start + timedelta(minutes=minutes),
                           last_leave=leave)


# ── what the post says ────────────────────────────────────────────────────────────────────────

def test_a_late_lesson_is_named_in_full(db, world):
    _late(world, 3)
    text = digest.digest_text(db, DAY, MORNING)
    assert "Штрафы за 17.09.2026" in text
    assert "Қайратқызы Дина" in text
    assert "NUET Sep 2" in text
    assert "13:00" in text                       # Almaty, not the stored UTC 08:00
    assert "Опоздание 3 мин — 600 ₸" in text
    assert "Всего за день: 600 ₸ · период 16–30 September 2026 открыт" in text


def test_minutes_that_were_worked_off_are_not_reported(db, world):
    """Joined three minutes late, stayed three minutes past the end: the lesson got its hour."""
    _late(world, 3, made_up=True)
    assert digest.digest_text(db, DAY, MORNING) is None


def test_a_waived_fine_is_not_reported(db, world):
    lesson = _late(world, 3)
    service.apply_decision(db, actor=world["head"], event_id=lesson.id,
                           teacher_id=world["teacher"].id, day=DAY, kind="late",
                           amount=0, reason_code="moved", note="перенесли")
    assert digest.digest_text(db, DAY, MORNING) is None


def test_a_missed_lesson_is_named_even_before_anybody_prices_it(db, world):
    """The most serious finding must not wait on a head teacher to reach the chat."""
    world["lesson"](first_join=None, last_leave=None)
    text = digest.digest_text(db, DAY, MORNING)
    assert "Урок не проведён — сумма не назначена" in text
    assert "без цены: 1" in text


def test_a_head_teachers_reason_travels_with_the_fine(db, world):
    lesson = _late(world, 5)
    service.apply_decision(db, actor=world["head"], event_id=lesson.id,
                           teacher_id=world["teacher"].id, day=DAY, kind="late",
                           amount=600, reason_code="technical", note="упал интернет")
    text = digest.digest_text(db, DAY, MORNING)
    assert "Технические проблемы: упал интернет — 600 ₸" in text


def test_a_clean_day_says_nothing_at_all(db, world):
    start = datetime(2026, 9, 17, 8, 0)
    world["lesson"](start=start, first_join=start, last_leave=start + timedelta(minutes=60))
    assert digest.digest_text(db, DAY, MORNING) is None


def test_a_group_name_cannot_smuggle_markup_into_the_chat(db, world):
    world["group"].name = "SAT <b>hack</b>"
    db.flush()
    _late(world, 2)
    text = digest.digest_text(db, DAY, MORNING)
    assert "SAT &lt;b&gt;hack&lt;/b&gt;" in text
    assert "<b>hack</b>" not in text


# ── corrections ───────────────────────────────────────────────────────────────────────────────

def test_a_fine_waived_after_its_post_comes_back_once_as_a_correction(db, world):
    """The chat is an archive: nothing is edited behind a reader's back."""
    lesson = world["lesson"](start=datetime(2026, 9, 16, 8, 0),
                             first_join=datetime(2026, 9, 16, 8, 2),
                             last_leave=datetime(2026, 9, 16, 9, 0))
    decision = service.apply_decision(db, actor=world["head"], event_id=lesson.id,
                                      teacher_id=world["teacher"].id, day=date(2026, 9, 16),
                                      kind="late", amount=0, reason_code="substitute",
                                      note=None, proposed_amount=400)
    decision.decided_at = MORNING - timedelta(hours=2)   # after yesterday's post, before this one
    db.flush()
    text = digest.digest_text(db, DAY, MORNING)
    assert "Отменено за прошлые дни" in text
    assert "16.09" in text
    assert "400 ₸ → 0 ₸" in text
    assert "Урок провёл другой преподаватель" in text


def test_a_correction_older_than_a_day_is_not_repeated(db, world):
    lesson = world["lesson"](start=datetime(2026, 9, 16, 8, 0),
                             first_join=datetime(2026, 9, 16, 8, 2),
                             last_leave=datetime(2026, 9, 16, 9, 0))
    decision = service.apply_decision(db, actor=world["head"], event_id=lesson.id,
                                      teacher_id=world["teacher"].id, day=date(2026, 9, 16),
                                      kind="late", amount=0, reason_code="substitute",
                                      proposed_amount=400)
    decision.decided_at = MORNING - timedelta(days=3)
    db.flush()
    assert digest.digest_text(db, DAY, MORNING) is None


# ── when it fires, and only once ──────────────────────────────────────────────────────────────

def test_nothing_is_posted_before_nine_in_the_morning(db, world):
    assert digest.due_day(datetime(2026, 9, 18, 3, 0)) is None      # 08:00 Almaty
    assert digest.due_day(datetime(2026, 9, 18, 4, 0)) == DAY       # 09:00 Almaty


def test_an_evening_tick_does_not_post_yesterdays_news_as_this_morning(db, world):
    assert digest.due_day(datetime(2026, 9, 18, 6, 0)) == DAY       # 11:00 Almaty, still morning
    assert digest.due_day(datetime(2026, 9, 18, 15, 0)) is None     # 20:00 Almaty


def test_nothing_is_posted_about_days_before_the_rule(db, world):
    assert digest.due_day(datetime(2026, 9, 16, 4, 0)) is None      # would be 15.09


def test_the_day_is_posted_once_however_often_the_tick_runs(db, world, posted):
    _late(world, 3)
    assert digest.send_if_due(db, MORNING) == "sent"
    assert digest.send_if_due(db, MORNING) is None
    assert digest.send_if_due(db, MORNING + timedelta(minutes=5)) is None
    assert len(posted) == 1
    assert posted[0]["group_id"] == 77
    assert posted[0]["topic_id"] == 9347
    assert posted[0]["key"] == "discipline-fines:2026-09-17"


def test_a_silent_day_is_recorded_as_looked_at_not_as_failed(db, world, posted):
    assert digest.send_if_due(db, MORNING) == "skipped"
    assert posted == []
    row = db.query(DisciplineDigestSend).filter(DisciplineDigestSend.day == DAY).one()
    assert (row.status, row.attempts) == ("skipped", 0)


def test_the_job_is_silent_when_the_chat_is_not_configured(db, world, monkeypatch):
    monkeypatch.delenv("DISCIPLINE_NOTICES_SUPPORT_GROUP_ID", raising=False)
    monkeypatch.delenv("MEET_NOTICES_SUPPORT_GROUP_ID", raising=False)
    assert digest.enabled() is False
    assert digest.run(db, MORNING) == {"status": "disabled"}


def test_the_topic_alone_is_enough_when_it_shares_the_staff_supergroup(db, world, monkeypatch):
    """«Штрафы учителя» is a topic in the same supergroup the Meet notices already use, so a
    deployment should not have to rediscover the group id to turn this on."""
    monkeypatch.delenv("DISCIPLINE_NOTICES_SUPPORT_GROUP_ID", raising=False)
    monkeypatch.setenv("MEET_NOTICES_SUPPORT_GROUP_ID", "77")
    assert digest.target() == (77, 9347)
    assert digest.enabled() is True


def test_a_failed_send_is_retried_and_then_given_up_on(db, world, monkeypatch):
    _late(world, 3)
    attempts = []

    def failing(group_id, text, key, **kw):
        attempts.append(key)
        return {"status": "failed", "error": "telegram is down"}

    monkeypatch.setattr(digest.outbox, "post", failing)
    for _ in range(5):
        digest.send_if_due(db, MORNING)
    assert len(attempts) == digest.MAX_ATTEMPTS
    row = db.query(DisciplineDigestSend).filter(DisciplineDigestSend.day == DAY).one()
    assert row.status == "failed"
    assert row.error == "telegram is down"


def test_the_period_is_named_as_closed_once_it_is(db, world, posted):
    _late(world, 3)
    service.close_period(db, period_containing(DAY), world["head"], now=MORNING)
    assert "период 16–30 September 2026 закрыт" in digest.digest_text(db, DAY, MORNING)
