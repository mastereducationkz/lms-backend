"""What the bot answers in a group's Telegram chat — and everything it must never say there.

The chat is a room full of students, so the answers are the group's own facts and nothing else:
its weekly timetable, the next lessons, homework deadlines, recordings, the weekly mock. Owner's
rules (2026-09-12, 2026-09-15), each enforced here rather than left to a model:

* **No person is ever named.** Not who handed the homework in, not how many did, not a mark, not
  a balance. A question about one person is answered with "write to me in private" and no data.
* **Recordings are LMS links** (``/recordings?watch=<event>``), which ask for a login. The
  login-free watch link exists for accountants and never goes into a chat.
* **Every answer is a template filled from the database** (:mod:`group_bot_render`). A very small
  model may only pick WHICH answer (:mod:`group_bot_intents`) when the rules cannot, and it never
  sees the facts — so neither a date nor a link can be invented, and a typed "ignore the above"
  can at most choose the wrong one of our own answers.
* **«Расписание» is the regular week; the next dated lessons are /lessons.** Every answer names
  the group.
* **A fact question always gets the facts**, including "nothing yet" — no homework is an answer,
  not a curator's task. Only a request the facts cannot serve (move a lesson, a broken link, an
  unknown question) goes to the group's curator, who is notified.

Support decides *when* the bot speaks (tagged, commands, rate limits, duplicates, deleting the
answer when the question is deleted); the LMS decides *what* it says.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_

from src.announcements.models import TelegramGroupLink, TelegramGroupQuestion
from src.messages.models import Notification
from src.schemas.models import Assignment, Event, EventGroup, Group, GroupAssignment, LessonRecording
from html import escape

from src.services import group_bot_intents as intents
from src.services import group_bot_keyboard as keyboard_ui
from src.services import group_bot_render as render
from src.services import group_bot_settings
from src.services.operational_groups import event_has_operational_group_clause
from src.services.recording_watch_links import lms_url

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
LESSONS_SHOWN = 5
SPAN_LESSONS_SHOWN = 14
HOMEWORK_SHOWN = 8          # fetched; the answer shows 5, open ones before overdue ones
RECORDINGS_SHOWN = 3
# A weekly set commonly opens early in the day and is asked about throughout the weekend. Keep
# the current set available for a few days, then prefer the next published one.
WEEKLY_TESTS_AHEAD = 3
WEEKLY_TEST_GRACE = timedelta(days=3)
# A deadline that has just passed is still the answer to "когда дедлайн?" — for a few days.
HOMEWORK_GRACE = timedelta(days=3)
# How far ahead the timetable looks to read the regular week and this week's changes.
SCHEDULE_HORIZON = timedelta(days=35)
RECENT_WINDOW = timedelta(days=7)


class NotLinked(Exception):
    """This Telegram chat is not any LMS group's — there is nothing to answer about."""


class SwitchedOff(Exception):
    """The bot is off, or this group is not in the pilot."""


def model_key() -> Optional[str]:
    return intents._model_key()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _digest(text: str) -> str:
    return hashlib.sha1((text or "").encode()).hexdigest()[:16]


def resolve(db, support_group_id: int) -> tuple[Group, bool]:
    """(group, is_test_chat). A linked chat, or a staff test chat pointed at a group's facts."""
    link = (db.query(TelegramGroupLink)
            .filter(TelegramGroupLink.support_group_id == support_group_id).first())
    group = db.get(Group, link.lms_group_id) if link else None
    if group is not None:
        return group, False
    test_group_id = group_bot_settings.test_chat_group(db, support_group_id)
    group = db.get(Group, test_group_id) if test_group_id else None
    if group is None:
        raise NotLinked(f"support group {support_group_id} is not linked to an LMS group")
    return group, True


def group_for(db, support_group_id: int) -> Group:
    return resolve(db, support_group_id)[0]


# ── the facts ────────────────────────────────────────────────────────────────────────────

def _lessons(db, group: Group, now: datetime):
    return (db.query(Event)
            .join(EventGroup, EventGroup.event_id == Event.id)
            .filter(EventGroup.group_id == group.id,
                    Event.is_active.is_(True),
                    Event.event_type == "class",
                    # An in-progress lesson still needs its Meet link.
                    Event.end_datetime > now,
                    event_has_operational_group_clause())
            .order_by(Event.start_datetime))


def _almaty_midnight(day: date) -> datetime:
    return datetime.combine(day, time()) - render.ALMATY_OFFSET


def span_window(span: str, now: datetime) -> tuple[datetime, datetime]:
    """[start, end) in naive UTC for a day or week named in Almaty terms."""
    today = render.local(now).date()
    monday = today + timedelta(days=7 - today.weekday())
    if span == "today":
        return now, _almaty_midnight(today + timedelta(days=1))
    if span == "tomorrow":
        return _almaty_midnight(today + timedelta(days=1)), _almaty_midnight(today + timedelta(days=2))
    if span == "weekend":
        # This Saturday and Sunday; on the weekend itself, what is left of it.
        saturday = today + timedelta(days=max(0, 5 - today.weekday()))
        return max(now, _almaty_midnight(saturday)), _almaty_midnight(monday)
    if span == "this_week":
        return now, _almaty_midnight(monday)
    if span == "next_week":
        return _almaty_midnight(monday), _almaty_midnight(monday + timedelta(days=7))
    return now, now + timedelta(days=7)


def _homework(db, group: Group, now: datetime):
    return (db.query(Assignment)
            .outerjoin(GroupAssignment, and_(GroupAssignment.assignment_id == Assignment.id,
                                             GroupAssignment.group_id == group.id,
                                             GroupAssignment.is_active.is_(True)))
            .filter(or_(Assignment.group_id == group.id, GroupAssignment.id.isnot(None)),
                    Assignment.is_active.is_(True),
                    Assignment.is_hidden.is_(False),
                    or_(Assignment.due_date.is_(None), Assignment.due_date >= now - HOMEWORK_GRACE))
            .order_by(Assignment.due_date.is_(None), Assignment.due_date)
            .limit(HOMEWORK_SHOWN)
            .all())


def _recordings(db, group: Group):
    rows = (db.query(Event, LessonRecording)
            .join(EventGroup, EventGroup.event_id == Event.id)
            .join(LessonRecording, LessonRecording.event_id == Event.id)
            .filter(EventGroup.group_id == group.id,
                    Event.is_active.is_(True),
                    LessonRecording.status == "ready",
                    LessonRecording.hls_url.isnot(None))
            .order_by(Event.start_datetime.desc())
            .limit(RECORDINGS_SHOWN)
            .all())
    return [(event, lms_url(f"/recordings?watch={event.id}")) for event, _recording in rows]


def _weekly_tests(db, group: Group, now: datetime):
    return (db.query(Event)
            .join(EventGroup, EventGroup.event_id == Event.id)
            .filter(EventGroup.group_id == group.id,
                    Event.is_active.is_(True),
                    Event.event_type == "weekly_test",
                    Event.start_datetime >= now - WEEKLY_TEST_GRACE,
                    event_has_operational_group_clause())
            .order_by(Event.start_datetime)
            .limit(WEEKLY_TESTS_AHEAD)
            .all())


def fact_answer(db, group: Group, intent: intents.Intent, now: datetime, lang: str) -> str:
    name = intent.name
    if name == "schedule":
        upcoming = _lessons(db, group, now).filter(Event.start_datetime < now + SCHEDULE_HORIZON).all()
        return render.schedule_answer(group, upcoming, now, lang)
    if name == "lessons" and intent.span:
        start, end = span_window(intent.span, now)
        query = _lessons(db, group, now).filter(Event.start_datetime < end)
        if start > now:
            query = query.filter(Event.start_datetime >= start)
        lessons = query.limit(SPAN_LESSONS_SHOWN).all()
        following = None if lessons else _lessons(db, group, now).filter(Event.start_datetime >= end).first()
        return render.lessons_answer(group, lessons, intent.span, following, now, lang)
    if name == "lessons":
        return render.lessons_answer(group, _lessons(db, group, now).limit(LESSONS_SHOWN).all(), None, None, now, lang)
    if name == "next":
        return render.next_answer(group, _lessons(db, group, now).first(), now, lang)
    if name == "homework":
        return render.homework_answer(group, _homework(db, group, now), now, lang, lms_url("/homework"))
    if name == "recording":
        return render.recordings_answer(group, _recordings(db, group), now, lang)
    if name == "weekly":
        return render.weekly_answer(group, _weekly_tests(db, group, now), now, lang)
    if name == "calendar":
        return calendar_answer(db, group, lang)
    raise ValueError(f"not a fact intent: {name}")


_CALENDAR_TEXT = {
    "ru": ("📆 Календарь группы — уроки, дедлайны и weekly mock появятся у вас в телефоне и будут "
           "обновляться сами:", "iPhone / Outlook (подписка)",
           "📆 Скоро здесь будет ссылка на календарь группы.", "скоро появится"),
    "kk": ("📆 Топ күнтізбесі — сабақтар, дедлайндар және weekly mock телефоныңызда өздігінен жаңарып тұрады:",
           "iPhone / Outlook (жазылу)", "📆 Жақында мұнда топ күнтізбесінің сілтемесі болады.", "жақында пайда болады"),
    "en": ("📆 Group calendar — lessons, deadlines and weekly mocks show up on your phone and stay up to date:",
           "iPhone / Outlook (subscribe)", "📆 A link to the group calendar will be here soon.", "coming soon"),
}


def calendar_answer(db, group: Group, lang: str) -> str:
    """Both links once the group's Google Calendar exists. Before that the ICS feed already works,
    so it is offered at once and the Google line says it is on its way — a student who uses
    Google should wait for the real calendar, which updates at once, not subscribe to the feed."""
    title, ics_label, soon, google_soon = _CALENDAR_TEXT.get(lang, _CALENDAR_TEXT["ru"])
    links = keyboard_ui.calendar_links(db, group)
    if not links:
        return render.join(render.header(group.name), soon)
    lines = [title]
    if links.get("google_url"):
        lines.append(f"• Google Calendar: {escape(links['google_url'])}")
    else:
        lines.append(f"• Google Calendar: {google_soon}")
    lines.append(f"• {ics_label}: {escape(links['ics_url'])}")
    return render.join(render.header(group.name), *lines)


# ── the answer ───────────────────────────────────────────────────────────────────────────

def _notify_curator(db, group: Group, question: str, chat_title: Optional[str], row_id: int) -> None:
    db.add(Notification(
        user_id=group.curator_id,
        title=f"Вопрос в чате группы {group.name}",
        content=f"{chat_title or group.name}: «{question.strip()[:400]}»\n"
                "Бот не смог ответить по данным LMS — ответьте, пожалуйста, в чате группы.",
        notification_type="group_bot_question",
        related_id=row_id,
    ))


def answer(db, *, support_group_id: int, text: str, command: Optional[str] = None,
           reply_to_text: Optional[str] = None, html: bool = False, chat_title: Optional[str] = None,
           telegram_chat_id: Optional[int] = None, message_id: Optional[int] = None,
           asker: Optional[dict] = None, now: Optional[datetime] = None,
           dry_run: bool = False) -> dict:
    """One message from a group chat → what the bot should say there.

    Raises :class:`NotLinked` (unknown chat) or :class:`SwitchedOff` (bot off; or the group is
    outside the pilot and this was not a command) before anything is read or written. A command
    in a linked group outside the pilot gets the "coming soon" notice, flagged ``not_live`` so
    Support posts it at most once a day. ``dry_run`` writes nothing and notifies nobody. The
    answer is Telegram HTML only when the caller asked for ``html``; otherwise plain text.

    ``dedupe_key`` is equal for two requests exactly when their answers are: Support stays quiet
    when the answer right before it in the chat was the same one.
    """
    now = now or _now()
    group, is_test_chat = resolve(db, support_group_id)
    if not group_bot_settings.enabled(db):
        raise SwitchedOff("the group bot is off")

    question = (text or "").strip()[:MAX_QUESTION_CHARS]
    command = command if command in intents.COMMANDS else None
    lang = "ru" if command else render.language(question)
    asker = asker or {}
    row = TelegramGroupQuestion(
        lms_group_id=group.id, support_group_id=support_group_id, telegram_chat_id=telegram_chat_id,
        chat_title=chat_title, message_id=message_id,
        asker_telegram_id=asker.get("telegram_user_id"), asker_username=asker.get("username"),
        asker_name=asker.get("name"), question=question, created_at=now,
    )

    def done(reply: Optional[str], intent: str, source: str, dedupe_key: Optional[str] = None, *,
             private_hint: bool = False, handed_to_curator: bool = False, not_live: bool = False,
             buttons: bool = False) -> dict:
        if reply is not None and not html:
            reply = render.to_plain(reply)
        row.answer, row.intent, row.model = reply, intent, source
        row.private_hint, row.handed_to_curator = private_hint, handed_to_curator
        if not dry_run:
            db.add(row)
            db.flush()
            if handed_to_curator:
                _notify_curator(db, group, question, chat_title, row.id)
            db.commit()
        return {"answer": reply, "format": "html" if html else "text", "intent": intent, "dedupe_key": dedupe_key,
                "silent": reply is None, "private_hint": private_hint,
                "handed_to_curator": handed_to_curator, "not_live": not_live,
                "group_name": group.name, "question_id": row.id,
                # The answer buttons (owner, 2026-09-15) — only under answers about the group's facts.
                "keyboard": keyboard_ui.keyboard(group.id) if buttons and reply is not None else None}

    if not group_bot_settings.enabled_for(db, group):
        if command is None:
            raise SwitchedOff(f"group {group.id} is outside the pilot")
        return done(render.plain(group, "not_live", lang, url=lms_url("/calendar")),
                    "not_live", "command", "not_live", not_live=True)

    intent = intents.classify(question, command=command, reply_to_text=reply_to_text)
    asked = _digest(intents.normalize(question))
    if intent.name in ("courtesy", "none"):
        # Addressed to us, but not a request. An audit row, and the chat stays quiet.
        return done(None, intent.name, intent.source)
    if intent.name == "personal":
        return done(render.plain(group, "private", lang), "personal", intent.source,
                    f"personal:{asked}", private_hint=True)
    if intent.name == "help":
        return done(render.plain(group, "help", lang), "help", intent.source, f"help:{lang}", buttons=True)
    if intent.name == "curator":
        reply = render.plain(group, "curator" if group.curator_id else "no_curator", lang)
        # A staff test chat reads like the real thing and never pages a real curator.
        return done(reply, "curator", intent.source, f"curator:{asked}",
                    handed_to_curator=bool(group.curator_id) and not is_test_chat)

    reply = fact_answer(db, group, intent, now, lang)
    return done(reply, intent.name, intent.source, f"{intent.name}:{intent.span or ''}:{_digest(reply)}",
                buttons=True)


def recent_count(db, now: Optional[datetime] = None) -> int:
    """How many questions the bot has been asked this week — the switch panel's one number."""
    now = now or _now()
    return (db.query(TelegramGroupQuestion)
            .filter(TelegramGroupQuestion.created_at >= now - RECENT_WINDOW).count())
