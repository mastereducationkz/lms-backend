"""The bot in a group's Telegram chat: what it answers there, and what it must never say.

The chat is a room full of students (owner, 2026-09-12), so many of these tests are about
silence: no names, no counts, no marks, no login-free links. Since 2026-09-15 every answer is a
template filled from the database and opens with the group's name; the model may only choose
which answer, and is faked or absent here.

Date-sensitive tests call :func:`group_bot.answer` with ``NOW`` = Monday 14 September 2026,
11:00 in Almaty (06:00 UTC).
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink, TelegramGroupQuestion
from src.assignments.models import Assignment
from src.events.models import LessonRecording
from src.messages.models import Notification
from src.routes.support_api import GroupQuestionAsker, GroupQuestionIn, answer_group_question
from src.services import group_bot, group_bot_intents, group_bot_settings
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures

SUPPORT_CHAT = 77
NOW = datetime(2026, 9, 14, 6, 0)
NAME = "IELTS July 8 2026 - Gulzada"
HEADER = f"📚 <b>{NAME}</b>"
MWF_2030 = {"start_date": "2026-08-17", "lessons_count": 36, "schedule_items": [
    {"day_of_week": d, "time_of_day": "20:30", "duration_minutes": 60} for d in (0, 2, 4)]}


@pytest.fixture
def chat(world, monkeypatch):
    """A pilot group with its chat linked and the bot on — and no model reachable."""
    db = world["db"]
    admin = _user(db, "admin")
    curator = _user(db, "curator")
    world["teacher"].workspace_email = "gulzada@mastereducation.kz"
    linked = world["group"](name=NAME, schedule_config=MWF_2030)
    linked.curator_id = curator.id
    world["enrol"](linked)
    db.add(TelegramGroupLink(lms_group_id=linked.id, support_group_id=SUPPORT_CHAT,
                             chat_title="IELTS July 8 - Gulzada"))
    db.flush()
    group_bot_settings.update(db, admin, enabled=True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(group_bot_intents, "_cache", group_bot_intents.OrderedDict())

    def ask(text, support_group_id=SUPPORT_CHAT, **fields):
        fields.setdefault("format", "html")
        return answer_group_question(
            GroupQuestionIn(support_group_id=support_group_id, text=text,
                            telegram_chat_id=-1001234567890, chat_title="IELTS July 8 - Gulzada",
                            message_id=4567,
                            asker=GroupQuestionAsker(telegram_user_id=777, username="aruzhan", name="Аружан"),
                            **fields),
            db=db)

    def at(text, **fields):
        fields.setdefault("support_group_id", SUPPORT_CHAT)
        fields.setdefault("html", True)
        return group_bot.answer(db, text=text, now=NOW, **fields)

    def lesson(day, hour=15, minute=30, **fields):
        start = datetime(2026, 9, day, hour, minute)
        return world["lesson"](linked, start_datetime=start, end_datetime=start + timedelta(hours=1), **fields)

    world.update(admin=admin, curator=curator, linked=linked, ask=ask, at=at, on=lesson)
    return world


def _rows(db):
    return db.query(TelegramGroupQuestion).order_by(TelegramGroupQuestion.id).all()


# ── the timetable and the lessons ────────────────────────────────────────────────────────

def test_schedule_is_the_regular_week_with_the_group_name(chat):
    for day in (14, 16, 18, 21):
        chat["on"](day, meeting_url="https://meet.google.com/abc-defg-hij")
    out = chat["at"]("/schedule", command="schedule")
    assert out["answer"].splitlines()[:3] == [HEADER, "🗓 Расписание группы (время Алматы):",
                                              "• Пн, Ср, Пт — 20:30–21:30"]
    assert "meet.google.com" not in out["answer"], "the timetable is days and times, not links"
    assert out["format"] == "html" and out["intent"] == "schedule" and out["group_name"] == NAME
    assert _rows(chat["db"])[0].intent == "schedule" and _rows(chat["db"])[0].model == "command"


def test_schedule_shows_this_weeks_changes(chat):
    chat["on"](14)
    chat["on"](16, hour=14, minute=0)
    chat["on"](21)
    text = chat["at"]("это постоянное расписание?")["answer"]
    assert "• Ср, 16 сентября — 19:00–20:00 вместо 20:30" in text
    assert "• Пт, 18 сентября, 20:30 — урока не будет" in text


def test_lessons_lists_the_next_five_dates_with_links(chat):
    for day in (14, 16, 18, 21, 23, 25):
        chat["on"](day, meeting_url=f"https://meet.google.com/day-{day}")
    text = chat["at"]("/lessons", command="lessons")["answer"]
    assert text.splitlines()[:3] == [HEADER, "📅 Ближайшие уроки (время Алматы):",
                                     "1. Сегодня, 14 сентября, 20:30–21:30"]
    assert "5. Ср, 23 сентября, 20:30–21:30" in text and "6." not in text
    assert "https://meet.google.com/day-14" in text and "day-25" not in text


def test_a_week_named_in_the_question_is_that_week(chat):
    for day in (14, 16, 18, 21, 23):
        chat["on"](day)
    out = chat["at"]("скинь пожалуйста расписание на следующую неделю")
    assert out["intent"] == "lessons"
    assert "📅 Уроки на следующей неделе" in out["answer"]
    assert "21 сентября" in out["answer"] and "23 сентября" in out["answer"]
    assert "16 сентября" not in out["answer"]


def test_the_weekend_is_this_saturday_and_sunday(chat):
    for day in (16, 19, 20, 21):
        chat["on"](day, hour=10, minute=0)
    text = chat["at"]("уроки есть на выходных?")["answer"]
    assert "📅 Уроки на выходных" in text
    assert "Сб, 19 сентября" in text and "Вс, 20 сентября" in text
    assert "16 сентября" not in text and "21 сентября" not in text


def test_no_weekend_lessons_still_say_when_the_next_one_is(chat):
    chat["on"](21, hour=10, minute=0)
    text = chat["at"]("на выходных будут занятия?")["answer"]
    assert "На выходных уроков нет." in text and "⏭ Следующий урок: Пн, 21 сентября" in text


def test_no_lesson_today_still_says_when_the_next_one_is(chat):
    chat["on"](16, meeting_url="https://meet.google.com/wed")
    text = chat["at"]("есть урок сегодня?")["answer"]
    assert "Сегодня уроков нет." in text
    assert "⏭ Следующий урок: Ср, 16 сентября, 20:30–21:30" in text and "wed" in text


def test_the_next_lesson_and_its_link(chat):
    chat["on"](15, meeting_url="https://meet.google.com/abc-defg-hij", topic="Reading: T/F/NG")
    out = chat["ask"]("когда следующий урок?")
    lines = out["answer"].splitlines()
    assert lines[0] == HEADER and lines[1].startswith("⏭ Следующий урок:")
    assert "Тема: Reading: T/F/NG" in out["answer"]
    assert "🔗 https://meet.google.com/abc-defg-hij" in out["answer"]
    row = _rows(chat["db"])[0]
    assert (row.intent, row.model, row.lms_group_id) == ("next", "rules", chat["linked"].id)
    assert row.asker_username == "aruzhan" and row.telegram_chat_id == -1001234567890


def test_an_in_progress_lesson_is_the_one_to_join(chat):
    start = datetime(2026, 9, 14, 5, 30)
    chat["lesson"](chat["linked"], start_datetime=start, end_datetime=start + timedelta(hours=1),
                   meeting_url="https://meet.google.com/current")
    text = chat["at"]("ссылка на урок")["answer"]
    assert "🟢 Урок идёт сейчас" in text and "current" in text


def test_a_link_request_never_falls_back_to_a_recording(chat):
    db = chat["db"]
    chat["on"](15, meeting_url="https://meet.google.com/upcoming-link")
    taught = chat["on"](7)
    db.add(LessonRecording(event_id=taught.id, status="ready", hls_url="videos/recordings/1/master.m3u8"))
    db.flush()
    text = chat["at"]("ссылка на урок")["answer"]
    assert "upcoming-link" in text and "/recordings?watch=" not in text


def test_a_lesson_of_another_group_is_never_in_the_answer(chat):
    other = chat["group"](name="SAT August 19 2026 - Gulzada")
    chat["enrol"](other)
    chat["lesson"](other, days_ahead=1, meeting_url="https://meet.google.com/xxx-yyyy-zzz")
    out = chat["ask"]("когда следующий урок?")
    assert "xxx-yyyy-zzz" not in out["answer"]
    assert "Ближайших уроков в расписании пока нет." in out["answer"]
    assert out["handed_to_curator"] is False, "an empty calendar is an answer, not a curator's task"


def test_a_finished_group_says_its_course_is_over(chat):
    chat["linked"].is_over = True
    chat["db"].flush()
    for command in ("next", "lessons", "schedule"):
        assert "Курс группы завершён" in chat["at"](f"/{command}", command=command)["answer"]


# ── homework, recordings, weekly mocks ───────────────────────────────────────────────────

def test_homework_is_titles_and_deadlines_and_nobodys_name(chat):
    db = chat["db"]
    student = chat["enrol"](chat["linked"])
    student.name = "Аяулым Сейтова"
    db.add(Assignment(group_id=chat["linked"].id, title="Reading Test 4", assignment_type="homework",
                      content="—", is_active=True, is_hidden=False, due_date=datetime(2026, 9, 16, 18, 59)))
    db.add(Assignment(group_id=chat["linked"].id, title="Essay <1>", assignment_type="homework",
                      content="—", is_active=True, is_hidden=False, due_date=datetime(2026, 9, 12, 18, 59)))
    db.flush()
    db.add(Assignment(group_id=chat["linked"].id, title="Listening 2", assignment_type="homework",
                      content="—", is_active=True, is_hidden=False, due_date=datetime(2026, 9, 14, 18, 59)))
    db.flush()
    text = chat["at"]("какое дз и до когда?")["answer"]
    assert "<b>Listening 2</b></a> — до 14 сентября, 23:59 (сегодня)" in text
    assert "<b>Reading Test 4</b></a> — до 16 сентября, 23:59" in text
    assert "<b>Essay &lt;1&gt;</b></a> — срок прошёл 12 сентября, 23:59" in text
    assert text.count('• <a href="https://lms.mastereducation.kz/homework/') == 3, "each title opens its homework"
    assert text.index("Listening 2") < text.index("Reading Test 4") < text.index("Essay"), \
        "open tasks come first, soonest on top; a passed deadline goes last"
    assert "/homework" in text
    assert "Аяулым" not in text
    for word in ("сдал", "не сдали", "1 из", "человек"):
        assert word not in text.lower()


def test_no_homework_is_an_answer_not_a_curator_task(chat):
    out = chat["ask"]("/homework", command="homework")
    assert "Открытых домашних заданий сейчас нет." in out["answer"]
    assert out["handed_to_curator"] is False
    assert chat["db"].query(Notification).count() == 0


def test_a_recording_is_the_lms_link_that_asks_for_a_login(chat):
    db = chat["db"]
    taught = chat["on"](11)
    db.add(LessonRecording(event_id=taught.id, status="ready", hls_url="videos/recordings/1/master.m3u8"))
    db.flush()
    text = chat["at"]("где запись урока?")["answer"]
    assert f"/recordings?watch={taught.id}" in text
    assert "/watch/" not in text and "master.m3u8" not in text


def test_no_recording_is_never_answered_with_the_next_lesson(chat):
    chat["on"](15, meeting_url="https://meet.google.com/next")
    text = chat["at"]("где запись урока?")["answer"]
    assert "Записей уроков пока нет." in text and "Следующий урок" not in text


def test_a_weekly_mock_uses_its_group_linked_calendar_event(chat):
    chat["on"](14, event_type="weekly_test", title="IELTS Weekly Test · 12.09-13.09",
               meeting_url="https://ielts.mastereducation.kz/weekly-sets/15")
    out = chat["at"]("когда будет следующий викли мок тест?")
    assert "IELTS Weekly Test · 12.09-13.09" in out["answer"]
    assert "https://ielts.mastereducation.kz/weekly-sets/15" in out["answer"]


def test_a_weekly_mock_says_whether_it_is_open_live_or_over(chat):
    chat["on"](12, hour=4, minute=0, event_type="weekly_test", title="Weekly · 12.09-13.09",
               meeting_url="https://ielts.mastereducation.kz/weekly-sets/15")
    chat["on"](14, hour=5, minute=0, event_type="weekly_test", title="Weekly · live",
               meeting_url="https://ielts.mastereducation.kz/weekly-sets/16")
    text = chat["at"]("/weekly", command="weekly")["answer"]
    assert "• Weekly · live — открыт до 14 сентября, 11:00 (сегодня)" in text
    assert "• Weekly · 12.09-13.09 — завершён" in text
    assert "weekly-sets/15" not in text, "a finished mock has no link to follow"
    assert text.index("Weekly · live") < text.index("12.09-13.09")


def test_no_weekly_mock_says_so(chat):
    assert "пока не опубликован" in chat["at"]("/weekly", command="weekly")["answer"]


# ── how it chooses ───────────────────────────────────────────────────────────────────────

def test_rules_and_commands_never_call_the_model(chat, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(group_bot_intents, "ask_model", lambda *a, **k: pytest.fail("the rules decide these"))
    chat["on"](15)
    for text in ("когда следующий урок?", "какое расписание?", "какое дз?", "келесі сабақ қашан?"):
        chat["at"](text)
    chat["at"]("/schedule", command="schedule")


def test_the_model_places_what_the_rules_cannot(chat, monkeypatch):
    monkeypatch.setattr(group_bot_intents, "ask_model", lambda question, context="": ("lessons", "gpt-4.1-nano"))
    chat["on"](16)
    out = chat["at"]("когда у нас уроки?")
    assert out["intent"] == "lessons" and "📅 Ближайшие уроки" in out["answer"]
    assert _rows(chat["db"])[0].model == "gpt-4.1-nano"


def test_without_a_model_an_ambiguous_lesson_question_gets_the_timetable(chat):
    chat["on"](16)
    out = chat["at"]("когда у нас уроки?")
    assert out["intent"] == "schedule" and _rows(chat["db"])[0].model == "default"


def test_a_follow_up_reply_keeps_the_topic(chat):
    chat["on"](15)
    out = chat["at"]("а завтра?", reply_to_text=f"{HEADER}\n📅 Уроки сегодня (время Алматы):\n1. …")
    assert out["intent"] == "lessons" and "Уроки завтра" in out["answer"]


def test_the_same_answer_has_the_same_dedupe_key(chat):
    chat["on"](16)
    assert chat["at"]("/schedule", command="schedule")["dedupe_key"] == chat["at"]("какое расписание?")["dedupe_key"]
    assert chat["at"]("/next", command="next")["dedupe_key"] == chat["at"]("когда следующий урок?")["dedupe_key"]
    assert chat["at"]("/next", command="next")["dedupe_key"] != chat["at"]("/lessons", command="lessons")["dedupe_key"]


def test_answers_follow_the_language_of_the_question(chat):
    chat["on"](15)
    assert "⏭ Келесі сабақ:" in chat["at"]("келесі сабақ қашан?")["answer"]
    assert "⏭ Next lesson:" in chat["at"]("when is the next lesson?")["answer"]
    assert "⏭ Следующий урок:" in chat["at"]("/next", command="next")["answer"]


def test_a_caller_that_did_not_ask_for_html_gets_plain_text(chat):
    """A Support that predates HTML answers must never show tags in a students' chat."""
    chat["linked"].name = "A <b> & C"
    chat["db"].flush()
    out = chat["ask"]("/next", command="next", format=None)
    assert out["format"] == "text"
    assert out["answer"].startswith("📚 A <b> & C\n") and "</b>" not in out["answer"]


def test_a_capabilities_question_lists_the_commands(chat):
    out = chat["ask"]("что ты умеешь?")
    assert out["answer"].startswith(HEADER) and "/schedule" in out["answer"] and "/lessons" in out["answer"]
    assert out["intent"] == "help"


@pytest.mark.parametrize("text", ["Пасыба", "отдуши брат", "сесе , понял", "спасибо 🙌"])
def test_a_thank_you_is_silent(chat, text):
    out = chat["ask"](text)
    assert out["silent"] is True and out["answer"] is None
    row = _rows(chat["db"])[0]
    assert row.answer is None and row.handed_to_curator is False and row.intent == "courtesy"


def test_a_courtesy_prefix_does_not_hide_a_real_request(chat):
    db = chat["db"]
    taught = chat["on"](11)
    db.add(LessonRecording(event_id=taught.id, status="ready", hls_url="videos/recordings/1/master.m3u8"))
    db.flush()
    out = chat["at"]("спасибо, скинь запись урока")
    assert out["silent"] is False and f"/recordings?watch={taught.id}" in out["answer"]


# ── what it refuses ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question", [
    "какой у меня балл?", "сколько у меня уроков на балансе", "я сдал домашку?",
    "когда мне оплатить обучение", "what is my balance?", "мой балл за мок",
])
def test_a_personal_question_is_answered_in_private_and_says_nothing(chat, question):
    chat["on"](15)
    out = chat["ask"](question)
    assert out["private_hint"] is True
    assert out["answer"] == f"{HEADER}\n" + group_bot.render.t("ru" if "my" not in question else "en", "private")
    row = _rows(chat["db"])[0]
    assert row.private_hint is True and row.intent == "personal"


def test_a_request_only_a_person_can_grant_goes_to_the_curator(chat):
    out = chat["ask"]("а можно перенести урок на другой день?")
    assert out["handed_to_curator"] is True
    assert "передал куратору" in out["answer"] and out["answer"].startswith(HEADER)
    note = chat["db"].query(Notification).filter_by(user_id=chat["curator"].id).one()
    assert note.notification_type == "group_bot_question"
    assert "перенести урок" in note.content and note.related_id == out["question_id"]


def test_without_a_curator_nobody_is_promised(chat):
    chat["linked"].curator_id = None
    chat["db"].flush()
    out = chat["ask"]("а можно перенести урок?")
    assert out["handed_to_curator"] is False and "куратора группы" in out["answer"]


# ── who it answers at all ────────────────────────────────────────────────────────────────

def test_an_unlinked_chat_is_not_answered(chat):
    with pytest.raises(HTTPException) as err:
        chat["ask"]("когда урок?", support_group_id=4242)
    assert err.value.status_code == 404
    assert _rows(chat["db"]) == [], "nothing is logged about a chat we know nothing about"


def test_the_switch_and_the_pilot_both_have_to_say_yes(chat):
    db = chat["db"]
    group_bot_settings.update(db, chat["admin"], enabled=False)
    for fields in ({}, {"command": "schedule"}):
        with pytest.raises(HTTPException) as err:
            chat["ask"]("когда урок?", **fields)
        assert err.value.status_code == 409

    group_bot_settings.update(db, chat["admin"], enabled=True)
    chat["teacher"].workspace_email = None      # out of the recording pilot
    db.flush()
    with pytest.raises(HTTPException) as err:
        chat["ask"]("когда урок?")
    assert err.value.status_code == 409

    group_bot_settings.update(db, chat["admin"], scope="all")
    chat["lesson"](chat["linked"], days_ahead=1)
    assert chat["ask"]("когда урок?")["answer"], "scope=all opens it to every linked chat"


def test_a_command_outside_the_pilot_gets_the_coming_soon_notice(chat):
    chat["teacher"].workspace_email = None
    chat["db"].flush()
    out = chat["ask"]("какое расписание?", command="schedule")
    assert out["not_live"] is True and out["dedupe_key"] == "not_live"
    assert out["answer"].startswith(HEADER) and "скоро заработает" in out["answer"]
    assert _rows(chat["db"])[0].intent == "not_live"


def test_the_pilot_rule_follows_the_teacher_who_teaches(chat):
    db = chat["db"]
    stand_in = chat["group"](name="IELTS June 1 2026 - Substitute")
    stand_in.teacher_id = _user(db, "teacher").id
    db.flush()
    assert group_bot_settings.in_pilot(db, stand_in) is False
    chat["lesson"](stand_in, days_ahead=1)      # taught by the world's pilot teacher
    assert group_bot_settings.in_pilot(db, stand_in) is True


def test_a_staff_test_chat_answers_about_its_group_and_never_pages_the_curator(chat):
    db = chat["db"]
    group_bot_settings.current(db)
    row = db.get(group_bot_settings.AppSetting, group_bot_settings.KEY)
    row.value = {**row.value, "test_chats": {"1": chat["linked"].id}}
    db.flush()
    chat["on"](15)
    assert chat["at"]("/next", command="next", support_group_id=1)["answer"].startswith(HEADER)
    out = chat["at"]("а можно перенести урок?", support_group_id=1)
    assert "передал куратору" in out["answer"] and out["handed_to_curator"] is False
    assert db.query(Notification).count() == 0


# ── the record ───────────────────────────────────────────────────────────────────────────

def test_every_question_is_written_down(chat):
    chat["on"](15)
    chat["ask"]("когда урок?")
    chat["ask"]("какой у меня балл?")
    chat["ask"]("а можно перенести урок?")
    rows = _rows(chat["db"])
    assert [r.intent for r in rows] == ["next", "personal", "curator"]
    assert all(r.chat_title == "IELTS July 8 - Gulzada" and r.message_id == 4567 for r in rows)
    assert all(r.answer for r in rows), "what the chat was told is part of the record"
    assert group_bot.recent_count(chat["db"]) == 3


def test_a_dry_run_writes_nothing_and_pages_nobody(chat):
    out = chat["at"]("а можно перенести урок?", dry_run=True)
    assert "передал куратору" in out["answer"] and out["question_id"] is None
    assert _rows(chat["db"]) == [] and chat["db"].query(Notification).count() == 0


def test_a_question_is_never_longer_than_the_column(chat):
    chat["ask"]("когда урок? " + "а" * 1500)
    assert len(_rows(chat["db"])[0].question) == group_bot.MAX_QUESTION_CHARS


def test_a_message_too_long_for_a_telegram_post_is_refused(chat):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):     # → 422 at the endpoint
        chat["ask"]("а" * 2500)


def test_the_switch_panel_reads_the_state(chat, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    described = group_bot_settings.describe(chat["db"])
    assert described["enabled"] is True and described["scope"] == "pilot"
    assert described["model_configured"] is True
    assert described["updated_by"] == chat["admin"].name
    assert described["enabled_at"].endswith("Z")
