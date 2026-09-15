"""Group bot v3 (owner, 2026-09-15): who is live, the answer buttons, the popup, the lesson link.

``NOW`` is Monday 14 September 2026, 11:00 Almaty (06:00 UTC), as in tests/test_group_bot.py.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink
from src.routes.group_bot_links import open_group_lesson
from src.routes.support_api import GroupPopupIn, group_button_popup
from src.schemas.models import AppSetting, Assignment
from src.services import group_bot_intents, group_bot_keyboard as kb, group_bot_popup as popup
from src.services import group_bot_settings
from tests.test_group_bot import HEADER, NOW, SUPPORT_CHAT, chat  # noqa: F401 - fixture
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures

TEACHER_EMAIL = "gulzada@mastereducation.kz"


def directory(db, *accounts):
    row = db.get(AppSetting, "workspace_directory")
    if row is None:
        row = AppSetting(key="workspace_directory")
        db.add(row)
    row.value = {"accounts": [{"email": email, "suspended": suspended} for email, suspended in accounts]}
    db.flush()


# ── live ─────────────────────────────────────────────────────────────────────────────────

def test_a_linked_running_group_with_a_connected_unsuspended_teacher_is_live(chat):
    directory(chat["db"], (TEACHER_EMAIL, False))
    chat["on"](15)
    assert group_bot_settings.is_live(chat["db"], chat["linked"], NOW) is True


@pytest.mark.parametrize("accounts", [[(TEACHER_EMAIL, True)], [("someone@mastereducation.kz", False)], None])
def test_a_suspended_absent_or_unknown_account_is_not_live(chat, accounts):
    if accounts is not None:
        directory(chat["db"], *accounts)
    chat["on"](15)
    assert group_bot_settings.is_live(chat["db"], chat["linked"], NOW) is False


def test_no_lesson_ahead_or_a_finished_group_is_not_live(chat):
    directory(chat["db"], (TEACHER_EMAIL, False))
    chat["on"](13)
    assert group_bot_settings.is_live(chat["db"], chat["linked"], NOW) is False
    chat["on"](16)
    chat["linked"].is_over = True
    chat["db"].flush()
    assert group_bot_settings.is_live(chat["db"], chat["linked"], NOW) is False


def test_a_group_whose_only_connected_teacher_is_a_substitute_is_not_live(chat):
    db = chat["db"]
    directory(db, (TEACHER_EMAIL, False))
    stand_in = chat["group"](name="August 6 SAT - Алпамыс")
    stand_in.teacher_id = _user(db, "teacher").id
    db.add(TelegramGroupLink(lms_group_id=stand_in.id, support_group_id=4545, chat_title="Aug 6"))
    db.flush()
    chat["lesson"](stand_in, start_datetime=datetime(2026, 9, 15, 15, 30), end_datetime=datetime(2026, 9, 15, 16, 30))
    assert group_bot_settings.in_pilot(db, stand_in) is True, "questions are still answered there"
    assert group_bot_settings.is_live(db, stand_in, NOW) is False


def test_an_unlinked_group_is_live_only_through_a_staff_test_chat(chat):
    db = chat["db"]
    directory(db, (TEACHER_EMAIL, False))
    solo = chat["group"](name="Unlinked")
    chat["lesson"](solo, start_datetime=datetime(2026, 9, 15, 15, 30), end_datetime=datetime(2026, 9, 15, 16, 30))
    assert group_bot_settings.is_live(db, solo, NOW) is False
    row = db.get(AppSetting, group_bot_settings.KEY)
    row.value = {**row.value, "test_chats": {"1": solo.id}}
    db.flush()
    assert group_bot_settings.is_live(db, solo, NOW) is True


# ── buttons ──────────────────────────────────────────────────────────────────────────────

def test_fact_and_help_answers_carry_the_buttons(chat):
    chat["on"](15)
    group_id = chat["linked"].id
    for text, fields in (("/schedule", {"command": "schedule"}), ("какое дз?", {}), ("что ты умеешь?", {})):
        rows = chat["at"](text, **fields)["keyboard"]
        assert rows == kb.keyboard(group_id)
    assert [b.get("callback") for b in rows[0] + rows[1][:1]] == ["gb:schedule", "gb:lessons", "gb:homework"]
    link = rows[1][1]["url"]
    assert link.startswith(f"https://lmsapi.mastereducation.kz/tg/l/{group_id}-") and len(link.rsplit("-", 1)[1]) == 16


@pytest.mark.parametrize("text", ["какой у меня балл?", "а можно перенести урок?", "спасибо"])
def test_personal_curator_and_silent_answers_have_no_buttons(chat, text):
    assert chat["at"](text)["keyboard"] is None


def test_the_telegram_markup_is_callback_data_and_url_buttons():
    markup = kb.to_telegram(kb.keyboard(7))
    assert markup["inline_keyboard"][0][0] == {"text": "🗓 Расписание", "callback_data": "gb:schedule"}
    assert set(markup["inline_keyboard"][1][1]) == {"text", "url"}
    assert markup["inline_keyboard"][1][1]["text"] == "🔗 Ближайший урок", "the link says which lesson it opens"


# ── the lesson link ──────────────────────────────────────────────────────────────────────

def _token(group_id):
    return f"{group_id}-{kb.lesson_link_sig(group_id)}"


def test_the_lesson_link_opens_the_running_lesson_then_the_next(chat):
    now = datetime.utcnow()
    running = chat["lesson"](chat["linked"], start_datetime=now - timedelta(minutes=20),
                             end_datetime=now + timedelta(minutes=40), meeting_url="https://meet.google.com/run-ning")
    chat["lesson"](chat["linked"], days_ahead=1, meeting_url="https://meet.google.com/nex-tone")
    response = open_group_lesson(_token(chat["linked"].id), db=chat["db"])
    assert response.status_code == 302 and response.headers["location"] == running.meeting_url

    running.meeting_url = None
    chat["db"].flush()
    assert open_group_lesson(_token(chat["linked"].id), db=chat["db"]).headers["location"] == \
        "https://meet.google.com/nex-tone"


def test_without_a_lesson_room_the_link_opens_the_lms_calendar(chat):
    chat["lesson"](chat["linked"], days_ahead=1, meeting_url="javascript:alert(1)")
    location = open_group_lesson(_token(chat["linked"].id), db=chat["db"]).headers["location"]
    assert location.endswith("/calendar")


@pytest.mark.parametrize("token", ["999999-0000000000000000", "abc", "12-zz", ""])
def test_a_forged_or_unknown_lesson_link_is_404(chat, token):
    with pytest.raises(HTTPException) as err:
        open_group_lesson(token, db=chat["db"])
    assert err.value.status_code == 404
    with pytest.raises(HTTPException):
        open_group_lesson(f"{chat['linked'].id}-{'0' * 16}", db=chat["db"])


# ── popups ───────────────────────────────────────────────────────────────────────────────

def _popup(chat, action):
    return popup.popup(chat["db"], support_group_id=SUPPORT_CHAT, action=action, now=NOW)


def test_the_schedule_popup_is_the_week_and_mentions_changes(chat):
    for day in (14, 16, 18, 21):
        chat["on"](day)
    assert _popup(chat, "schedule") == {"text": "🗓 Расписание:\nПн, Ср, Пт — 20:30–21:30", "show_alert": True}
    chat["on"](19, hour=10, minute=0)           # an extra Saturday lesson
    assert _popup(chat, "schedule")["text"] == (
        "🗓 Расписание:\nПн, Ср, Пт — 20:30–21:30\n\nНа этой неделе есть изменения — /schedule")


def test_the_lessons_popup_lists_dates_that_fit(chat):
    for day in (14, 16, 18, 21, 23, 25):
        chat["on"](day)
    text = _popup(chat, "lessons")["text"]
    assert text == "📅 Ближайшие уроки:\nСегодня 20:30\nСр 16.09 20:30\nПт 18.09 20:30\nПн 21.09 20:30\nСр 23.09 20:30"
    assert popup.fits(text)


def test_the_homework_popup_stays_under_telegrams_limit_with_long_emoji_titles(chat):
    db = chat["db"]
    for n in range(7):
        db.add(Assignment(group_id=chat["linked"].id, title=f"🔥📚 Очень длинное название задания номер {n} 🧠✨🚀",
                          assignment_type="homework", content="—", is_active=True, is_hidden=False,
                          due_date=datetime(2026, 9, 14, 18, 59) + timedelta(days=n)))
    db.flush()
    text = _popup(chat, "homework")["text"]
    assert popup.units(text) <= 200
    assert text.startswith("📝 Домашние задания:\n• 🔥📚 Очень длинное название задания")
    assert "(сегодня)" in text and "…" in text and "\n+ ещё" in text and text.endswith("— /homework")
    assert all(line.startswith("• ") for line in text.split("\n")[1:-1]), "one task per line"
    text.encode("utf-8")          # no lone surrogate anywhere


def test_empty_popups_say_so(chat):
    assert _popup(chat, "homework")["text"] == "📝 Открытых заданий нет"
    assert _popup(chat, "lessons")["text"] == "📅 Ближайших уроков пока нет"


def test_the_popup_endpoint_refuses_what_it_cannot_answer(chat):
    def call(**fields):
        body = {"support_group_id": SUPPORT_CHAT, "action": "schedule", **fields}
        return group_button_popup(GroupPopupIn(**body), db=chat["db"])

    with pytest.raises(HTTPException) as err:
        call(action="delete_everything")
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:
        call(support_group_id=4242)
    assert err.value.status_code == 404
    group_bot_settings.update(chat["db"], chat["admin"], enabled=False)
    with pytest.raises(HTTPException) as err:
        call()
    assert err.value.status_code == 409


def test_cap_never_exceeds_the_limit_and_never_splits_an_emoji():
    text = popup.cap("😀" * 300)
    assert popup.units(text) <= 200
    text.encode("utf-8")


# ── calendar ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["добавьте уроки в календарь", "calendar", "ics ссылка", "күнтізбе бар ма?"])
def test_calendar_questions_are_their_own_intent(text):
    assert group_bot_intents.classify(text, use_model=False).name == "calendar"


def test_the_calendar_answer_says_soon_when_there_are_no_links(chat, monkeypatch):
    monkeypatch.setattr(kb, "calendar_links", lambda db, group: None)
    out = chat["at"]("/calendar", command="calendar")
    assert out["intent"] == "calendar"
    assert out["answer"] == f"{HEADER}\n📆 Скоро здесь будет ссылка на календарь группы."
    assert out["keyboard"] == kb.keyboard(chat["linked"].id)


def test_before_the_google_calendar_exists_the_feed_is_offered_and_google_is_coming(chat, monkeypatch):
    monkeypatch.setattr(kb, "calendar_links", lambda db, group: {
        "google_url": None, "ics_url": "https://lmsapi.mastereducation.kz/cal/g.ics"})
    text = chat["at"]("/calendar", command="calendar")["answer"]
    assert "• Google Calendar — скоро появится" in text
    assert '• <a href="https://lmsapi.mastereducation.kz/cal/g.ics">iPhone / Outlook (подписка)</a>' in text


def test_a_hyperlink_survives_as_label_and_url_for_a_plain_text_caller():
    from src.services import group_bot_render as render

    html = '📚 <b>A &amp; B</b>\n• <a href="https://calendar.google.com/x?cid=1&amp;hl=ru">Google Calendar</a>'
    assert render.to_plain(html) == "📚 A & B\n• Google Calendar: https://calendar.google.com/x?cid=1&hl=ru"


def test_the_calendar_answer_gives_both_links(chat, monkeypatch):
    monkeypatch.setattr(kb, "calendar_links", lambda db, group: {
        "google_url": "https://calendar.google.com/calendar/r?cid=abc", "ics_url": "https://lmsapi.mastereducation.kz/cal/g.ics"})
    text = chat["at"]("как добавить в календарь?")["answer"]
    assert '• <a href="https://calendar.google.com/calendar/r?cid=abc">Google Calendar</a>' in text
    assert '• <a href="https://lmsapi.mastereducation.kz/cal/g.ics">iPhone / Outlook (подписка)</a>' in text
    assert "https://calendar.google.com" not in text.replace('href="https://calendar.google.com', ""), \
        "the links are hyperlinks, not bare URLs"
