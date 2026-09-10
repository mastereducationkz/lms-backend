"""Lesson invitations in the group's Telegram chat: the text, the matching, the job, the links.

Owner decisions (2026-09-10): plain post, 5 minutes before, only lessons in an LMS Meet room,
links suggested by name and confirmed by a person. Support's bot is faked here: the job's
contract with it is the request it makes and how it reads each kind of answer.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.announcements.models import TelegramGroupLink, TelegramLessonInvitation
from src.announcements.routes import telegram_links
from src.services import support_client, telegram_invitations as ti
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures

MEET = "https://meet.google.com/nee-tsrk-vap"


# --- the text ------------------------------------------------------------------------------


def test_the_text_is_the_copy_buttons_text():
    """Same five lines as lms-front src/lib/meetLinks.test.ts pins for the lesson card."""
    text = ti.invitation_text("July 8 SAT - Gulzada: Lesson 29", ["July 8 SAT - Gulzada"],
                              datetime(2026, 9, 10, 14, 0), datetime(2026, 9, 10, 15, 0), MEET)
    assert text == "\n".join([
        "Приглашение на урок",
        "July 8 SAT, урок 29",
        "Четверг, 10 сентября, 19:00–20:00 (время Алматы)",
        f"Google Meet: {MEET}",
        "Подключайтесь за пару минут до начала.",
    ])


def test_the_text_is_safe_for_telegram_html():
    text = ti.invitation_text("A & B <x>: Lesson 1", ["A & B <x>"], datetime(2026, 9, 10, 14),
                              datetime(2026, 9, 10, 15), MEET)
    assert "A &amp; B &lt;x&gt;, урок 1" in text


def test_a_lesson_without_a_number_still_reads():
    text = ti.invitation_text("Mock exam", [], datetime(2026, 9, 10, 14), datetime(2026, 9, 10, 15), MEET)
    assert text.splitlines()[1] == "Mock exam"


# --- matching chats to groups --------------------------------------------------------------


@pytest.mark.parametrize("title", ["SAT July 8 | Gulzada", "July 8 SAT", "SAT 8 июля Gulzada", "Master Education — July 8 SAT (chat)"])
def test_the_same_group_in_other_words_matches(title):
    assert ti.match_score("July 8 SAT - Gulzada", title) >= ti.SUGGESTION_THRESHOLD


@pytest.mark.parametrize("title", ["July 18 SAT - Gulzada", "IELTS August 4 2026", "SAT July 22"])
def test_a_different_group_does_not(title):
    assert ti.match_score("July 8 SAT - Gulzada", title) < ti.SUGGESTION_THRESHOLD


def test_a_leading_start_date_in_the_chat_title_is_not_the_groups_number():
    """Real titles (2026-09-10): "07.08 SAT August 6 2026" is the chat of "August 6 SAT - …".
    Read as numbers, "07" and "08" contradicted the 6, and 16 such chats got no suggestion."""
    assert ti.match_score("August 6 SAT - Нурай", "07.08 SAT August 6 2026") >= ti.SUGGESTION_THRESHOLD
    assert ti.match_score("August 7 SAT - Нурай", "07.08 SAT August 6 2026") < ti.SUGGESTION_THRESHOLD


def test_leading_zeros_do_not_matter():
    assert ti.match_score("SAT August 6", "SAT August 06") >= ti.SUGGESTION_THRESHOLD


def test_each_chat_is_suggested_once_and_the_clearest_match_wins():
    groups = [(1, "July 8 SAT - Gulzada"), (2, "July 8 SAT")]
    chats = [(10, "July 8 SAT - Gulzada"), (11, "IELTS August 4")]
    chosen = ti.suggest_links(groups, chats)
    assert chosen[1][0] == 10 and 2 not in chosen


def test_linked_groups_and_chats_are_not_suggested_again():
    assert ti.suggest_links([(1, "July 8 SAT")], [(10, "July 8 SAT")], taken_chats={10}) == {}
    assert ti.suggest_links([(1, "July 8 SAT")], [(10, "July 8 SAT")], taken_groups={1}) == {}


# --- the job -------------------------------------------------------------------------------


@pytest.fixture
def setup(world, monkeypatch):
    monkeypatch.setenv("ENABLE_TELEGRAM_LESSON_INVITES", "1")
    db = world["db"]
    world["teacher"].workspace_email = "gulzada@mastereducation.kz"
    group = world["group"](name="July 8 SAT - Gulzada")
    world["enrol"](group)
    db.add(TelegramGroupLink(lms_group_id=group.id, support_group_id=77, chat_title="July 8 SAT"))
    db.flush()
    calls = []

    def fake(answer=None, error=None):
        def _call(method, path, **kwargs):
            calls.append((method, path, kwargs["json_body"]))
            if error:
                raise error
            return answer or {"status": "sent", "telegram_message_id": 555}
        monkeypatch.setattr(ti.support_client, "call", _call)

    fake()

    def lesson(minutes_ahead=4, **fields):
        fields.setdefault("meeting_url", MEET)
        return world["lesson"](group, days_ahead=minutes_ahead / 1440,
                               title="July 8 SAT - Gulzada: Lesson 30", **fields)

    return {"db": db, "world": world, "group": group, "calls": calls, "fake": fake, "lesson": lesson}


def _run(setup):
    return ti.send_due_invitations(setup["db"], datetime.utcnow())


def _row(setup, ev):
    return setup["db"].query(TelegramLessonInvitation).filter_by(event_id=ev.id).one_or_none()


def test_a_lesson_in_five_minutes_is_announced_once(setup):
    ev = setup["lesson"]()
    assert _run(setup)["sent"] == 1
    method, path, body = setup["calls"][0]
    assert (method, path) == ("POST", "/telegram/messages")
    assert body["telegram_group_id"] == 77
    assert body["idempotency_key"] == f"lesson-invite:{ev.id}:{setup['group'].id}"
    assert body["text"].startswith("Приглашение на урок\nJuly 8 SAT, урок 30\n")
    assert f"Google Meet: {MEET}" in body["text"]
    assert _row(setup, ev).status == "sent" and _row(setup, ev).telegram_message_id == 555

    _run(setup)
    assert len(setup["calls"]) == 1, "never twice"


def test_not_yet_due_and_long_started_lessons_are_left_alone(setup):
    setup["lesson"](minutes_ahead=20)
    setup["lesson"](minutes_ahead=-15)
    assert _run(setup) == {"sent": 0, "failed": 0, "skipped": 0}


def test_a_lesson_without_an_lms_meet_room_is_not_announced(setup):
    setup["lesson"](meeting_url=None)
    setup["lesson"](meeting_url="https://zoom.us/j/123")
    setup["world"]["teacher"].workspace_email = None
    setup["lesson"]()  # a Meet link, but not one of our rooms
    setup["db"].flush()
    assert _run(setup)["sent"] == 0


def test_a_substitute_still_gets_the_rooms_invitation(setup):
    """The room belongs to the lesson (made for the group's onboarded teacher)."""
    sub = _user(setup["db"], "teacher")
    setup["lesson"](teacher_id=sub.id)
    assert _run(setup)["sent"] == 1


def test_an_unlinked_group_is_not_announced(setup):
    setup["db"].query(TelegramGroupLink).delete()
    setup["lesson"]()
    assert _run(setup)["sent"] == 0 and setup["calls"] == []


def test_a_stopped_group_is_not_announced(setup):
    setup["group"].is_active = False
    setup["lesson"]()
    assert _run(setup)["sent"] == 0


def test_an_unapproved_chat_is_skipped_for_good(setup):
    ev = setup["lesson"]()
    setup["fake"](error=HTTPException(status_code=409, detail="Group is not approved"))
    assert _run(setup)["skipped"] == 1
    _run(setup)
    assert len(setup["calls"]) == 1 and _row(setup, ev).status == "skipped"


def test_a_transient_failure_is_retried_up_to_three_times(setup):
    ev = setup["lesson"]()
    setup["fake"](error=HTTPException(status_code=503, detail="try later"))
    for _ in range(5):
        _run(setup)
    assert len(setup["calls"]) == ti.MAX_ATTEMPTS
    assert _row(setup, ev).status == "failed" and _row(setup, ev).attempts == ti.MAX_ATTEMPTS


def test_support_unreachable_counts_as_transient(setup):
    setup["lesson"]()
    setup["fake"](error=HTTPException(status_code=502, detail=support_client.UNREACHABLE_DETAIL))
    _run(setup)
    _run(setup)
    assert len(setup["calls"]) == 2


def test_a_permanent_telegram_refusal_is_not_retried(setup):
    ev = setup["lesson"]()
    setup["fake"](error=HTTPException(status_code=502, detail="Forbidden: bot was kicked from the group chat"))
    _run(setup)
    _run(setup)
    assert len(setup["calls"]) == 1 and _row(setup, ev).status == "failed"


def test_a_retry_after_a_lost_answer_is_safe(setup):
    """A crash between claim and answer leaves 'pending'; the retry reuses the same key."""
    ev = setup["lesson"]()
    setup["db"].add(TelegramLessonInvitation(event_id=ev.id, lms_group_id=setup["group"].id,
                                             support_group_id=77, status="pending", attempts=1))
    setup["db"].flush()
    setup["fake"](answer={"status": "duplicate", "telegram_message_id": 555})
    _run(setup)
    assert setup["calls"][0][2]["idempotency_key"] == f"lesson-invite:{ev.id}:{setup['group'].id}"
    assert _row(setup, ev).status == "sent"


def test_nothing_happens_when_switched_off(setup, monkeypatch):
    monkeypatch.delenv("ENABLE_TELEGRAM_LESSON_INVITES")
    setup["lesson"]()
    assert _run(setup) == {"sent": 0, "failed": 0, "skipped": 0} and setup["calls"] == []


# --- linking -------------------------------------------------------------------------------


@pytest.fixture
def linking(world, monkeypatch):
    db = world["db"]
    admin = _user(db, "admin")
    sat = world["group"](name="July 8 SAT - Gulzada")
    ielts = world["group"](name="IELTS June 14 2026 - Шадеева")
    world["enrol"](sat)
    world["enrol"](ielts)
    chats = [{"id": 10, "title": "SAT July 8", "status": "approved", "is_active": True},
             {"id": 11, "title": "Random chat", "status": "approved", "is_active": True},
             {"id": 12, "title": "Old chat", "status": "approved", "is_active": False}]
    monkeypatch.setattr(telegram_links.support_client, "call", lambda *a, **k: chats)
    return {"db": db, "admin": admin, "sat": sat, "ielts": ielts}


def _groups(response):
    return {g["id"]: g for g in response["groups"]}


def test_the_list_suggests_the_matching_chat(linking):
    groups = _groups(telegram_links.list_links(db=linking["db"], current_user=linking["admin"]))
    assert groups[linking["sat"].id]["suggestion"]["chat_id"] == 10
    assert groups[linking["ielts"].id]["suggestion"] is None


def test_inactive_chats_are_never_offered(linking):
    response = telegram_links.list_links(db=linking["db"], current_user=linking["admin"])
    assert {c["id"] for c in response["chats"]} == {10, 11}


def test_confirming_links_and_the_suggestion_disappears(linking):
    telegram_links.confirm_links(
        body=telegram_links.ConfirmBody(pairs=[{"lms_group_id": linking["sat"].id, "support_group_id": 10}]),
        db=linking["db"], current_user=linking["admin"])
    sat = _groups(telegram_links.list_links(db=linking["db"], current_user=linking["admin"]))[linking["sat"].id]
    assert sat["link"]["chat_id"] == 10 and sat["suggestion"] is None


def test_an_unapproved_chat_cannot_be_linked(linking):
    with pytest.raises(HTTPException) as refused:
        telegram_links.set_link(linking["sat"].id, telegram_links.LinkBody(support_group_id=12),
                                db=linking["db"], current_user=linking["admin"])
    assert refused.value.status_code == 422


def test_a_link_can_be_removed(linking):
    telegram_links.set_link(linking["sat"].id, telegram_links.LinkBody(support_group_id=10),
                            db=linking["db"], current_user=linking["admin"])
    telegram_links.set_link(linking["sat"].id, telegram_links.LinkBody(support_group_id=None),
                            db=linking["db"], current_user=linking["admin"])
    assert linking["db"].query(TelegramGroupLink).count() == 0


def test_the_screen_still_works_when_support_is_down(linking, monkeypatch):
    def down(*a, **k):
        raise HTTPException(status_code=502, detail=support_client.UNREACHABLE_DETAIL)
    monkeypatch.setattr(telegram_links.support_client, "call", down)
    response = telegram_links.list_links(db=linking["db"], current_user=linking["admin"])
    assert response["chats_error"] and response["groups"]
