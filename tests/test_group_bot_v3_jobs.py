"""Group bot v3 (owner, 2026-09-15): what the bot says on its own.

The hello, the pinned timetable, the regular-schedule change notice, the digest and the last-chance
reminder. Support is faked at :mod:`group_bot_outbox`; everything else is the real database.
Times are naive UTC; Almaty is UTC+5.
"""
import hashlib
from datetime import datetime, timedelta

import pytest

from src.announcements.models import (
    TelegramDigestSend, TelegramGroupGreeting, TelegramPinnedTimetable, TelegramScheduleWatch,
)
from src.schemas.models import AppSetting, Assignment
from src.services import group_bot_digest as digest
from src.services import group_bot_hello as hello
from src.services import group_bot_hello_texts as texts
from src.services import group_bot_jobs, group_bot_keyboard as kb
from src.services import group_bot_outbox as outbox
from src.services import group_bot_pinned as pinned
from src.services import group_bot_schedule_watch as watch
from src.services import group_bot_settings
from tests.test_group_bot import MWF_2030, NAME, NOW, chat  # noqa: F401 - fixture
from tests.test_group_bot_v3_buttons import TEACHER_EMAIL, directory
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


@pytest.fixture
def live(chat, monkeypatch):
    """The chat fixture made live, with Support faked."""
    directory(chat["db"], (TEACHER_EMAIL, False))
    chat["on"](21)
    calls = {"post": [], "edit": [], "outcome": "sent", "edit_result": {"ok": True, "gone": False}}

    def post(support_group_id, text, key, *, silent, pin=False, reply_markup=None):
        calls["post"].append({"sg": support_group_id, "text": text, "key": key, "silent": silent,
                              "pin": pin, "reply_markup": reply_markup})
        status = calls["outcome"]
        return {"status": status, "telegram_message_id": 1000 + len(calls["post"]) if status == "sent" else None,
                "error": None if status == "sent" else "503: retry"}

    def edit(support_group_id, message_id, text, reply_markup):
        calls["edit"].append({"message_id": message_id, "text": text})
        return calls["edit_result"]

    monkeypatch.setattr(outbox, "post", post)
    monkeypatch.setattr(outbox, "edit", edit)
    chat["calls"] = calls
    chat["tick"] = lambda job, now: job.run(chat["db"], outbox.live_links(chat["db"], now), outbox.Budget(), now)
    return chat


def _setting(db, **values):
    row = db.get(AppSetting, group_bot_settings.KEY)
    row.value = {**row.value, **values}
    db.flush()


# ── hello ────────────────────────────────────────────────────────────────────────────────

def test_the_group_hello_is_announcement_7_verbatim_and_the_individual_one_is_neutral():
    assert hashlib.md5(texts.GROUP_HELLO.encode()).hexdigest()[:8] == "7c2f1aef"
    for gendered in ("отвлекся", "отвлеклась", "не успел", "не успела", "понял", "поняла", "забыла"):
        assert gendered not in texts.INDIVIDUAL_HELLO
    assert "@mastereducation_support_bot" in texts.INDIVIDUAL_HELLO


def test_the_hello_waits_for_the_owner_and_the_flag(live, monkeypatch):
    assert hello.enabled(live["db"]) is False
    monkeypatch.setenv("ENABLE_TELEGRAM_AUTO_HELLO", "1")
    assert hello.enabled(live["db"]) is False
    _setting(live["db"], auto_hello_enabled=True)
    assert hello.enabled(live["db"]) is True


def test_a_live_chat_is_greeted_once(live):
    live["tick"](hello, NOW)
    live["tick"](hello, NOW + timedelta(minutes=5))
    assert len(live["calls"]["post"]) == 1
    sent = live["calls"]["post"][0]
    assert sent["text"] == texts.GROUP_HELLO and sent["key"] == f"hello:{live['linked'].id}" and sent["silent"] is False
    row = live["db"].query(TelegramGroupGreeting).one()
    assert (row.status, row.source, row.variant, row.sent_at) == ("sent", "auto", "group", NOW)


def test_an_individual_chat_gets_the_neutral_hello(live):
    live["linked"].group_type = "individual"
    live["db"].flush()
    live["tick"](hello, NOW)
    assert live["calls"]["post"][0]["text"] == texts.INDIVIDUAL_HELLO


def test_a_backfilled_or_failing_hello_is_never_repeated_endlessly(live):
    live["calls"]["outcome"] = "failed"
    for minute in range(5):
        live["tick"](hello, NOW + timedelta(minutes=minute))
    assert len(live["calls"]["post"]) == outbox.MAX_ATTEMPTS
    assert live["db"].query(TelegramGroupGreeting).one().status == "failed"


def test_a_chat_that_is_not_live_is_not_greeted(live):
    directory(live["db"], (TEACHER_EMAIL, True))
    live["tick"](hello, NOW)
    assert live["calls"]["post"] == []


# ── pinned timetable ─────────────────────────────────────────────────────────────────────

def _greet(db, group, sent_at):
    db.add(TelegramGroupGreeting(lms_group_id=group.id, support_group_id=77, variant="group", source="backfill",
                                 status="sent", attempts=0, created_at=sent_at, sent_at=sent_at))
    db.flush()


def test_the_timetable_is_pinned_silently_a_minute_after_the_hello(live):
    for day in (14, 16, 18):
        live["on"](day, meeting_url="https://meet.google.com/abc-defg-hij")
    live["tick"](pinned, NOW)
    assert live["calls"]["post"] == [], "no hello yet"
    _greet(live["db"], live["linked"], NOW - timedelta(seconds=30))
    live["tick"](pinned, NOW)
    assert live["calls"]["post"] == [], "the hello is not a minute old"
    live["tick"](pinned, NOW + timedelta(seconds=45))
    [sent] = live["calls"]["post"]
    assert sent["pin"] is True and sent["silent"] is True and sent["key"] == f"pinned-timetable:{live['linked'].id}"
    assert sent["text"].startswith(f"📚 <b>{NAME}</b>\n🗓 Расписание группы") and "⏭ Следующий урок" in sent["text"]
    assert sent["reply_markup"] == kb.to_telegram(kb.keyboard(live["linked"].id))
    row = live["db"].query(TelegramPinnedTimetable).one()
    assert (row.status, row.telegram_message_id) == ("posted", 1001)


def test_the_pinned_timetable_is_edited_only_when_it_would_change_and_respects_removal(live):
    live["on"](16)
    _greet(live["db"], live["linked"], NOW - timedelta(hours=1))
    live["tick"](pinned, NOW)
    live["tick"](pinned, NOW)
    assert (len(live["calls"]["post"]), len(live["calls"]["edit"])) == (1, 0)

    live["on"](15, hour=10, minute=0)               # a lesson appears: the message would change
    live["tick"](pinned, NOW)
    assert len(live["calls"]["edit"]) == 1 and live["calls"]["edit"][0]["message_id"] == 1001

    live["calls"]["edit_result"] = {"ok": False, "gone": True}
    live["on"](17, hour=10, minute=0)
    live["tick"](pinned, NOW)
    row = live["db"].query(TelegramPinnedTimetable).one()
    assert row.status == "removed" and row.removed_at == NOW
    live["on"](19, hour=10, minute=0)
    live["tick"](pinned, NOW)
    assert (len(live["calls"]["post"]), len(live["calls"]["edit"])) == (1, 2), "never re-posted or re-pinned"


def test_the_pinned_timetable_offers_the_calendar_when_there_is_one(live, monkeypatch):
    monkeypatch.setattr(kb, "calendar_links", lambda db, group: {"google_url": "https://calendar.google.com/x",
                                                                 "ics_url": "https://lms/x.ics"})
    _greet(live["db"], live["linked"], NOW - timedelta(hours=1))
    live["tick"](pinned, NOW)
    last_row = live["calls"]["post"][0]["reply_markup"]["inline_keyboard"][-1]
    assert last_row == [{"text": "📆 Добавить в календарь", "url": "https://calendar.google.com/x"}]


# ── the regular week changed ─────────────────────────────────────────────────────────────

MON_THU_1930 = {**MWF_2030, "schedule_items": [
    {"day_of_week": d, "time_of_day": "19:30", "duration_minutes": 60} for d in (0, 1, 2, 3)]}


def _reschedule(chat, config):
    chat["linked"].schedule_config = config
    chat["db"].flush()


def test_a_changed_week_is_announced_once_it_settles(live):
    live["tick"](watch, NOW)
    assert live["calls"]["post"] == [] and live["db"].query(TelegramScheduleWatch).count() == 1

    _reschedule(live, MON_THU_1930)
    live["tick"](watch, NOW + timedelta(minutes=1))
    live["tick"](watch, NOW + timedelta(minutes=10))
    assert live["calls"]["post"] == [], "an admin may still be editing"
    live["tick"](watch, NOW + timedelta(minutes=17))
    [sent] = live["calls"]["post"]
    assert sent["text"] == (f"📚 <b>{NAME}</b>\n⚠️ Расписание поменялось!\n\nБыло:\n• Пн, Ср, Пт — 20:30–21:30\n\n"
                            "Стало:\n• Пн–Чт — 19:30–20:30\n\n"
                            "Актуальное расписание всегда в закреплённом сообщении и по /schedule")
    assert sent["key"].startswith(f"schedule-change:{live['linked'].id}:")
    live["tick"](watch, NOW + timedelta(minutes=40))
    assert len(live["calls"]["post"]) == 1


def test_a_change_undone_before_it_settles_is_never_announced(live):
    live["tick"](watch, NOW)
    _reschedule(live, MON_THU_1930)
    live["tick"](watch, NOW + timedelta(minutes=1))
    _reschedule(live, MWF_2030)
    for minute in (5, 20, 40):
        live["tick"](watch, NOW + timedelta(minutes=minute))
    assert live["calls"]["post"] == []
    assert live["db"].query(TelegramScheduleWatch).one().pending_json is None


def test_a_change_settling_at_night_is_announced_at_eight(live):
    night = datetime(2026, 9, 14, 18, 0)          # 23:00 Almaty
    live["tick"](watch, night)
    _reschedule(live, MON_THU_1930)
    live["tick"](watch, night + timedelta(minutes=1))
    live["tick"](watch, night + timedelta(minutes=30))
    assert live["calls"]["post"] == []
    live["tick"](watch, datetime(2026, 9, 15, 3, 0))  # 08:00 Almaty
    assert len(live["calls"]["post"]) == 1


# ── digest and last chance ───────────────────────────────────────────────────────────────

AT_1005 = datetime(2026, 9, 14, 5, 5)    # 10:05 Almaty


def _homework(chat, title, due, **fields):
    task = Assignment(group_id=chat["linked"].id, title=title, assignment_type="homework", content="—",
                      is_active=True, is_hidden=False, due_date=due, **fields)
    chat["db"].add(task)
    chat["db"].flush()
    return task


def test_the_morning_digest_is_the_days_lessons_without_the_group_name(live):
    live["on"](14, meeting_url="https://meet.google.com/abc-defg-hij")
    live["tick"](digest, AT_1005)
    live["tick"](digest, AT_1005 + timedelta(minutes=3))
    [sent] = live["calls"]["post"]
    text = sent["text"]
    assert text.split("\n\n")[0] in digest.GREETINGS and text.split("\n\n")[-1] in digest.CLOSERS
    assert "📚 Урок в 20:30–21:30 — не проспи 😴\n🔗 https://meet.google.com/abc-defg-hij" in text
    assert NAME not in text and sent["key"] == f"digest:morning:{live['linked'].id}:2026-09-14"


def test_no_digest_before_ten_or_on_an_empty_day(live):
    live["on"](14)
    live["tick"](digest, datetime(2026, 9, 14, 4, 30))      # 09:30
    live2 = datetime(2026, 9, 15, 5, 5)                     # Tuesday 10:05: nothing that day
    live["tick"](digest, live2)
    assert live["calls"]["post"] == []


def test_an_early_lesson_gets_its_digest_the_evening_before(live):
    live["on"](14, hour=3, minute=0)                        # 08:00 Almaty
    live["tick"](digest, AT_1005)
    assert live["calls"]["post"] == [], "the morning digest would come after the lesson"
    live["tick"](digest, datetime(2026, 9, 13, 15, 5))      # Sunday 20:05
    [sent] = live["calls"]["post"]
    assert sent["text"].startswith("🌙 Напоминалка на завтра: урок уже в 08:00, не проспи 😴\n\n📚 Урок в 08:00–09:00\n")
    assert sent["key"] == f"digest:evening:{live['linked'].id}:2026-09-14"


def test_a_deadline_alone_is_worth_a_digest(live):
    _homework(live, "Essay <2>", datetime(2026, 9, 14, 18, 59))
    live["tick"](digest, AT_1005)
    [sent] = live["calls"]["post"]
    assert "📝 Дедлайны:\n• <b>Essay &lt;2&gt;</b> — сегодня до 23:59" in sent["text"]


def test_a_group_switched_off_gets_no_digest(live):
    live["on"](14)
    _setting(live["db"], digest_off_groups=[live["linked"].id])
    live["tick"](digest, AT_1005)
    assert live["calls"]["post"] == []


def test_the_rotation_is_stable_per_day_and_varies_across_days():
    assert digest.pick(digest.GREETINGS, 5, "2026-09-14", "greeting") == digest.pick(digest.GREETINGS, 5, "2026-09-14", "greeting")
    days = {digest.pick(digest.GREETINGS, 5, f"2026-09-{d:02d}", "greeting") for d in range(1, 20)}
    assert len(days) >= 3


def test_the_last_chance_comes_three_hours_before_the_deadline(live):
    task = _homework(live, "Reading Test 4", datetime(2026, 9, 14, 15, 0))      # 20:00 Almaty
    live["tick"](digest, datetime(2026, 9, 14, 11, 0))
    assert live["calls"]["post"] == []
    live["tick"](digest, datetime(2026, 9, 14, 12, 5))
    [sent] = live["calls"]["post"]
    assert sent["text"] == "⏰ До дедлайна 3 часа: <b>Reading Test 4</b> — до 20:00. Кто ещё не сдал — самое время 🏃"
    assert sent["key"] == f"digest:last_chance:{live['linked'].id}:{task.id}:2026-09-14T15:00:00"

    task.due_date = datetime(2026, 9, 14, 16, 0)                                 # moved by an hour
    live["db"].flush()
    live["tick"](digest, datetime(2026, 9, 14, 13, 5))
    assert len(live["calls"]["post"]) == 2


def test_a_last_chance_falling_in_quiet_hours_waits_for_eight_and_says_the_real_time_left(live):
    _homework(live, "Vocabulary", datetime(2026, 9, 15, 4, 30))                 # 09:30 Almaty
    live["tick"](digest, datetime(2026, 9, 15, 2, 0))                           # 07:00: quiet
    assert live["calls"]["post"] == []
    live["tick"](digest, datetime(2026, 9, 15, 3, 0))                           # 08:00
    assert live["calls"]["post"][0]["text"].startswith("⏰ До дедлайна 1 час 30 минут: <b>Vocabulary</b> — до 09:30.")


def test_a_deadline_that_passes_during_the_night_gets_no_reminder(live):
    _homework(live, "Night task", datetime(2026, 9, 14, 20, 30))                # 01:30 Almaty
    live["tick"](digest, datetime(2026, 9, 14, 18, 30))                         # 23:30
    live["tick"](digest, datetime(2026, 9, 15, 3, 0))
    assert live["calls"]["post"] == []


@pytest.mark.parametrize("delta,expected", [(timedelta(hours=3), "3 часа"), (timedelta(minutes=170), "3 часа"),
                                            (timedelta(minutes=121), "2 часа"), (timedelta(minutes=45), "45 минут"),
                                            (timedelta(minutes=61), "1 час"), (timedelta(minutes=2), "5 минут")])
def test_time_left_reads_naturally(delta, expected):
    assert digest.time_left(delta) == expected


# ── the tick ─────────────────────────────────────────────────────────────────────────────

def test_nothing_runs_without_its_flag(live, monkeypatch):
    for name in ("ENABLE_TELEGRAM_AUTO_HELLO", "ENABLE_TELEGRAM_PINNED_TIMETABLE",
                 "ENABLE_TELEGRAM_SCHEDULE_CHANGE_NOTICES", "ENABLE_TELEGRAM_DIGEST"):
        monkeypatch.delenv(name, raising=False)
    live["on"](14)
    assert group_bot_jobs.run_tick(live["db"], AT_1005) == {}
    monkeypatch.setenv("ENABLE_TELEGRAM_DIGEST", "1")
    summary = group_bot_jobs.run_tick(live["db"], AT_1005)
    assert set(summary) == {"digest"} and summary["digest"]["sent"] == 1


def test_a_tick_makes_at_most_its_budget_of_support_calls(live):
    budget = outbox.Budget(calls=1)
    live["on"](14)
    _homework(live, "A", datetime(2026, 9, 14, 8, 0))
    digest.run(live["db"], outbox.live_links(live["db"], AT_1005), budget, AT_1005)
    assert len(live["calls"]["post"]) == 1
    assert live["db"].query(TelegramDigestSend).count() >= 1
