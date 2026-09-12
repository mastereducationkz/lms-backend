"""The bot in a group's Telegram chat: what it answers there, and what it must never say.

The chat is a room full of students (owner, 2026-09-12), so most of these tests are about
silence: no names, no counts, no marks, no login-free links. The model is faked — what is
asserted is the facts it is handed and the rules around it, which is where the rules live.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink, TelegramGroupQuestion
from src.assignments.models import Assignment
from src.events.models import LessonRecording
from src.messages.models import Notification
from src.routes.support_api import GroupQuestionAsker, GroupQuestionIn, answer_group_question
from src.services import group_bot, group_bot_settings
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures

SUPPORT_CHAT = 77


@pytest.fixture
def chat(world, monkeypatch):
    """A pilot group with its chat linked and the bot on — and no model reachable by default."""
    db = world["db"]
    admin = _user(db, "admin")
    curator = _user(db, "curator")
    world["teacher"].workspace_email = "gulzada@mastereducation.kz"
    linked = world["group"](name="IELTS July 8 2026 - Gulzada")
    linked.curator_id = curator.id
    world["enrol"](linked)
    db.add(TelegramGroupLink(lms_group_id=linked.id, support_group_id=SUPPORT_CHAT,
                             chat_title="IELTS July 8 - Gulzada"))
    db.flush()
    group_bot_settings.update(db, admin, enabled=True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def ask(text, support_group_id=SUPPORT_CHAT):
        return answer_group_question(
            GroupQuestionIn(support_group_id=support_group_id, text=text,
                            telegram_chat_id=-1001234567890, chat_title="IELTS July 8 - Gulzada",
                            message_id=4567,
                            asker=GroupQuestionAsker(telegram_user_id=777, username="aruzhan",
                                                     name="Аружан")),
            db=db)

    world.update(admin=admin, curator=curator, linked=linked, ask=ask)
    return world


def _rows(db):
    return db.query(TelegramGroupQuestion).order_by(TelegramGroupQuestion.id).all()


# ── what it answers ──────────────────────────────────────────────────────────────────────

def test_it_answers_from_the_groups_own_lessons(chat):
    chat["lesson"](chat["linked"], days_ahead=1,
                   meeting_url="https://meet.google.com/abc-defg-hij")
    out = chat["ask"]("когда следующий урок?")
    assert out["private_hint"] is False and out["handed_to_curator"] is False
    assert "Ближайший урок" in out["answer"]
    assert "https://meet.google.com/abc-defg-hij" in out["answer"]
    row = _rows(chat["db"])[0]
    assert (row.question, row.model, row.lms_group_id) == (
        "когда следующий урок?", "facts", chat["linked"].id)
    assert row.asker_username == "aruzhan" and row.telegram_chat_id == -1001234567890


def test_homework_is_titles_and_deadlines_and_nobodys_name(chat):
    db = chat["db"]
    chat["lesson"](chat["linked"], days_ahead=1)
    student = chat["enrol"](chat["linked"])
    student.name = "Аяулым Сейтова"
    db.add(Assignment(group_id=chat["linked"].id, title="Reading Test 4",
                      assignment_type="homework", content="—", is_active=True, is_hidden=False,
                      due_date=datetime.utcnow() + timedelta(days=2)))
    db.flush()
    answer = chat["ask"]("какое дз и до когда?")["answer"]
    assert "Reading Test 4" in answer
    assert "Аяулым" not in answer, "the group chat never hears a student's name"
    for word in ("сдал", "не сдали", "1 из", "человек"):
        assert word not in answer.lower()


def test_a_recording_is_the_lms_link_that_asks_for_a_login(chat):
    db = chat["db"]
    taught = chat["lesson"](chat["linked"], days_ahead=-1)
    db.add(LessonRecording(event_id=taught.id, status="ready",
                           hls_url="videos/recordings/1/master.m3u8"))
    db.flush()
    answer = chat["ask"]("где запись урока?")["answer"]
    assert f"/recordings?watch={taught.id}" in answer
    assert "/watch/" not in answer, "the login-free link is for accountants, never for a chat"


def test_the_model_writes_the_answer_when_it_can(chat, monkeypatch):
    chat["lesson"](chat["linked"], days_ahead=1)
    seen = {}

    def fake(facts, question, key):
        seen.update(facts=facts, question=question, key=key)
        return "Келесі сабақ — ертең 17:00-де.", True

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(group_bot, "_ask_model", fake)
    out = chat["ask"]("келесі сабақ қашан?")
    assert out["answer"] == "Келесі сабақ — ертең 17:00-де."
    assert _rows(chat["db"])[0].model == group_bot.MODEL
    assert seen["question"] == "келесі сабақ қашан?"
    assert set(seen["facts"]) == {"группа", "преподаватель", "куратор", "сейчас",
                                  "ближайшие_уроки", "домашние_задания", "записи_уроков"}


def test_the_model_is_handed_no_student_and_no_free_link(chat, monkeypatch):
    """The containment: whatever a student types, the model has nothing personal to leak."""
    db = chat["db"]
    taught = chat["lesson"](chat["linked"], days_ahead=-1)
    db.add(LessonRecording(event_id=taught.id, status="ready", hls_url="videos/r/1/master.m3u8"))
    named = chat["enrol"](chat["linked"])
    named.name = "Аяулым Сейтова"
    db.add(Assignment(group_id=chat["linked"].id, title="Reading Test 4",
                      assignment_type="homework", content="—", is_active=True, is_hidden=False))
    db.flush()
    facts = group_bot.group_facts(db, chat["linked"])
    blob = repr(facts)
    assert "Аяулым" not in blob and str(named.id) not in blob
    assert "/watch/" not in blob and "master.m3u8" not in blob
    assert facts["записи_уроков"][0]["ссылка"].endswith(f"/recordings?watch={taught.id}")


def test_a_model_that_does_not_answer_falls_back_to_the_facts(chat, monkeypatch):
    chat["lesson"](chat["linked"], days_ahead=1)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(group_bot, "_ask_model", lambda *a, **k: (None, False))
    assert "Ближайший урок" in chat["ask"]("когда урок?")["answer"]
    assert _rows(chat["db"])[0].model == "facts"


def test_a_capabilities_question_describes_scope_instead_of_the_next_lesson(chat):
    chat["lesson"](chat["linked"], days_ahead=1)

    out = chat["ask"]("что ты умеешь?")

    assert "расписанием" in out["answer"]
    assert "Ближайший урок" not in out["answer"]
    assert _rows(chat["db"])[0].model == "facts"


def test_a_thank_you_reply_is_silent(chat):
    chat["lesson"](chat["linked"], days_ahead=1)

    out = chat["ask"]("Пасыба")

    assert out["silent"] is True and out["answer"] is None
    row = _rows(chat["db"])[0]
    assert row.answer is None and row.handed_to_curator is False


def test_a_weekly_mock_question_is_handed_to_the_curator_not_answered_as_a_lesson(chat):
    chat["lesson"](chat["linked"], days_ahead=1)

    out = chat["ask"]("когда будет следующий викли мок тест?")

    assert out["handed_to_curator"] is True
    assert out["answer"] == group_bot.CURATOR_REPLY


# ── what it refuses ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question", [
    "какой у меня балл?",
    "сколько у меня уроков на балансе",
    "я сдал домашку?",
    "когда мне оплатить обучение",
    "what is my balance?",
])
def test_a_personal_question_is_answered_in_private_and_says_nothing(chat, question):
    chat["lesson"](chat["linked"], days_ahead=1)
    out = chat["ask"](question)
    assert out["private_hint"] is True
    assert out["answer"] == group_bot.PRIVATE_REPLY
    row = _rows(chat["db"])[0]
    assert row.private_hint is True and row.model is None


@pytest.mark.parametrize("question", ["когда следующий урок?", "какое дз?", "где запись?",
                                      "когда дедлайн по эссе?"])
def test_group_questions_are_not_mistaken_for_personal_ones(chat, question):
    assert group_bot.is_personal(question) is False


def test_a_question_the_facts_do_not_answer_goes_to_the_curator(chat):
    db = chat["db"]
    empty = chat["group"](name="IELTS September 9 2026 - Gulzada")
    empty.curator_id = chat["curator"].id
    db.add(TelegramGroupLink(lms_group_id=empty.id, support_group_id=99, chat_title="IELTS Sept"))
    db.flush()

    out = chat["ask"]("а можно перенести урок на другой день?", support_group_id=99)
    assert out["handed_to_curator"] is True
    assert out["answer"] == group_bot.CURATOR_REPLY
    note = db.query(Notification).filter_by(user_id=chat["curator"].id).one()
    assert note.notification_type == "group_bot_question"
    assert "перенести урок" in note.content and note.related_id == out["question_id"]


def test_an_unrelated_question_with_lessons_still_goes_to_the_curator(chat):
    chat["lesson"](chat["linked"], days_ahead=1)
    out = chat["ask"]("а можно перенести урок на другой день?")
    assert out["handed_to_curator"] is True
    assert out["answer"] == group_bot.CURATOR_REPLY


# ── who it answers at all ────────────────────────────────────────────────────────────────

def test_an_unlinked_chat_is_not_answered(chat):
    with pytest.raises(HTTPException) as err:
        chat["ask"]("когда урок?", support_group_id=4242)
    assert err.value.status_code == 404
    assert _rows(chat["db"]) == [], "nothing is logged about a chat we know nothing about"


def test_the_switch_and_the_pilot_both_have_to_say_yes(chat):
    db = chat["db"]
    group_bot_settings.update(db, chat["admin"], enabled=False)
    with pytest.raises(HTTPException) as err:
        chat["ask"]("когда урок?")
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


def test_the_pilot_rule_follows_the_teacher_who_teaches(chat):
    """A group whose own teacher has no Workspace account is still in the pilot if the teacher
    of its lessons has one — the same test that decides whether a lesson gets a Meet room."""
    db = chat["db"]
    stand_in = chat["group"](name="IELTS June 1 2026 - Substitute")
    stand_in.teacher_id = _user(db, "teacher").id
    db.flush()
    assert group_bot_settings.in_pilot(db, stand_in) is False
    chat["lesson"](stand_in, days_ahead=1)      # taught by the world's pilot teacher
    assert group_bot_settings.in_pilot(db, stand_in) is True


def test_a_lesson_of_another_group_is_never_in_the_answer(chat):
    other = chat["group"](name="SAT August 19 2026 - Gulzada")
    chat["enrol"](other)
    chat["lesson"](other, days_ahead=1, meeting_url="https://meet.google.com/xxx-yyyy-zzz")
    out = chat["ask"]("когда следующий урок?")
    assert "xxx-yyyy-zzz" not in out["answer"]
    assert out["handed_to_curator"] is True, "this group itself has nothing scheduled"


# ── the record ───────────────────────────────────────────────────────────────────────────

def test_every_question_is_written_down(chat):
    chat["lesson"](chat["linked"], days_ahead=1)
    chat["ask"]("когда урок?")
    chat["ask"]("какой у меня балл?")
    chat["ask"]("а можно перенести урок?", support_group_id=SUPPORT_CHAT)
    rows = _rows(chat["db"])
    assert [r.private_hint for r in rows] == [False, True, False]
    assert all(r.chat_title == "IELTS July 8 - Gulzada" and r.message_id == 4567 for r in rows)
    assert all(r.answer for r in rows), "what the chat was told is part of the record"
    assert group_bot.recent_count(chat["db"]) == 3


def test_a_question_is_never_longer_than_the_column(chat):
    chat["lesson"](chat["linked"], days_ahead=1)
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
