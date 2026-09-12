"""What the bot answers in a group's Telegram chat — and everything it must never say there.

The chat is a room full of students, so the answers are the group's own facts and nothing else:
when the next lessons are, what homework is due, where last week's recording is. Owner's rules
(2026-09-12), each of them enforced here rather than left to the model:

* **No person is ever named.** Not who handed the homework in, not how many did, not a mark, not
  a balance. A question about one person is answered with "write to me in private", where the
  support bot's 1:1 assistant already answers such things — this endpoint returns no data at all.
* **Recordings are LMS links** (``/recordings?watch=<event>``), which ask for a login. The
  login-free watch link exists for accountants and never goes into a chat.
* **The facts are built here, from the database.** The model only turns them into a sentence: it
  is given the bundle and the question and has no tools, so an instruction typed by a student
  ("ignore the above and tell me…") can change the wording and never the data.
* **When the facts do not answer it**, the bot says a curator will come back and the group's
  curator gets a notification — a question is never quietly dropped.

Support decides *when* the bot speaks (only when tagged, rate-limited); the LMS decides *what*
it says. The same split as lesson invitations.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_

from src.announcements.models import TelegramGroupLink, TelegramGroupQuestion
from src.messages.models import Notification
from src.schemas.models import (
    Assignment,
    Event,
    EventGroup,
    Group,
    GroupAssignment,
    LessonRecording,
    UserInDB,
)
from src.services import group_bot_settings
from src.services.operational_groups import event_has_operational_group_clause
from src.services.recording_watch_links import lms_url
from src.services.telegram_invitations import RU_MONTHS, RU_WEEKDAYS, _almaty

logger = logging.getLogger(__name__)

MODEL = os.getenv("GROUP_BOT_MODEL", "gpt-4o-mini")
MODEL_URL = "https://api.openai.com/v1/chat/completions"
MODEL_TIMEOUT_SECONDS = 20
MAX_QUESTION_CHARS = 1000

LESSONS_AHEAD = 5
HOMEWORK_SHOWN = 6
RECORDINGS_SHOWN = 3
# A deadline that has just passed is still the answer to "когда дедлайн?" — the day after, it is
# not the group's business any more.
HOMEWORK_GRACE = timedelta(days=3)

RECENT_WINDOW = timedelta(days=7)


class NotLinked(Exception):
    """This Telegram chat is not any LMS group's — there is nothing to answer about."""


class SwitchedOff(Exception):
    """The bot is off, or this group is not in the pilot."""


# What only the person themselves may be told. Deliberately about *possession* — «мой балл»,
# «сколько у меня» — so that «когда дедлайн» stays a group question while «я сдал?» does not.
_PERSONAL = re.compile(
    r"(мо[ияёе]\w*|меня|мне|my|мен(?:ің|ин)|маған)\s*\w{0,12}\s*"
    r"(балл|оцен|балан|долг|оплат|платеж|платёж|дз|домашк|задани|посещаем|пропуск|счет|счёт|"
    r"mark|grade|balance|payment|homework)"
    r"|(сколько|как[ао]й|что)\s+(у\s+меня|мо[ияёе]\w*)"
    r"|я\s+(сдал|сдала|оплатил|оплатила|должен|должна|пропустил|пропустила)"
    r"|(оплат|платеж|платёж|балан|задолжен)\w*\s+(мо[ияёе]\w*|за\s+меня)",
    re.IGNORECASE,
)

PRIVATE_REPLY = (
    "Это личный вопрос — по баллам, оплате и своим заданиям напишите мне в личные сообщения, "
    "там отвечу."
)
CURATOR_REPLY = "Не могу ответить на это здесь — передал куратору группы, с вами свяжутся."
NOTHING_YET = "Пока нечего показать по этой группе — расписание и задания появятся здесь."

# A reply to one of the bot's announcements is deliberately an invocation, but
# it is not necessarily a question.  These messages should leave the room
# quiet: a bot replying to «спасибо» is more intrusive than helpful.
_COURTESY = re.compile(
    r"^\s*(?:спасибо|спс|пас\w*|рахмет|рақмет|thanks|thx|ok|okay|понятно|ясно|"
    r"👍|🙏|❤|❤️|\+)\s*[!.…]*\s*$",
    re.IGNORECASE,
)
_CAPABILITIES = re.compile(
    r"(?:что|ч[её])\s+(?:ты\s+)?умеешь|(?:на\s+какие\s+)?вопросы\s+"
    r"(?:ты\s+)?(?:можешь\s+)?отвечать|what\s+can\s+you\s+do|"
    r"сен\s+не\s+істей\s+аласың|не\s+істей\s+аласың",
    re.IGNORECASE,
)
# Weekly mocks/tests are not in the facts supplied to this bot.  Naming one
# must never accidentally fall through to the next ordinary lesson.
_UNSUPPORTED_TOPIC = re.compile(
    r"\b(?:мок|mock|викл\w*|weekly|пробн\w*|тест\w*|exam\w*|экзам\w*)",
    re.IGNORECASE,
)
_OUT_OF_SCOPE_REQUEST = re.compile(
    r"перенес\w*|отмен\w*|замен\w*|reschedul\w*|cancel\w*|change\s+(?:the\s+)?lesson",
    re.IGNORECASE,
)
_SCHEDULE_TOPIC = re.compile(
    r"(?:урок\w*|занят\w*|расписани\w*|встреч\w*|созвон\w*|meet|"
    r"сабақ\w*|кесте\w*|lesson\w*|schedule\w*)|"
    r"(?:(?:когда|во\s+сколько|қашан|қай\s+кезде|when|what\s+time).{0,40}"
    r"(?:следующ|ближайш|next|upcoming|келесі))",
    re.IGNORECASE,
)
_HOMEWORK_TOPIC = re.compile(
    r"дз|домашк\w*|домашн\w*|задани\w*|дедлайн\w*|homework|үй\s+тапсырма",
    re.IGNORECASE,
)
_RECORDING_TOPIC = re.compile(r"запис\w*|recording\w*|видео|жазба", re.IGNORECASE)

SYSTEM_PROMPT = (
    "Ты — помощник учебного центра Master Education в групповом чате учеников.\n"
    "Отвечай ТОЛЬКО по данным из блока ФАКТЫ. Ничего не придумывай: ни дат, ни ссылок, ни имён.\n"
    "Никогда не называй учеников по именам и не говори, кто сдал или сколько сдали.\n"
    "Отвечай коротко — одно-три предложения, без приветствий и подписей.\n"
    "Отвечай на языке вопроса (русский, казахский или английский).\n"
    "Если в фактах нет ответа, верни answered=false и пустой answer.\n"
    'Ответ строго в JSON: {"answered": true|false, "answer": "..."}'
)


def model_key() -> Optional[str]:
    value = os.getenv("OPENAI_API_KEY")
    return value.strip() if value and value.strip() else None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_personal(text: str) -> bool:
    """A question about one person — answered in private, never here."""
    return bool(_PERSONAL.search(text or ""))


def group_for(db, support_group_id: int) -> Group:
    link = (db.query(TelegramGroupLink)
            .filter(TelegramGroupLink.support_group_id == support_group_id).first())
    group = db.get(Group, link.lms_group_id) if link else None
    if group is None:
        raise NotLinked(f"support group {support_group_id} is not linked to an LMS group")
    return group


# ── the facts ────────────────────────────────────────────────────────────────────────────

def _when(start: datetime, end: Optional[datetime]) -> str:
    a = _almaty(start)
    day = f"{RU_WEEKDAYS[a.weekday()]}, {a.day} {RU_MONTHS[a.month - 1]}"
    if end is None:
        return f"{day}, {a:%H:%M}"
    return f"{day}, {a:%H:%M}–{_almaty(end):%H:%M}"


def _date(value: datetime) -> str:
    a = _almaty(value)
    return f"{a.day} {RU_MONTHS[a.month - 1]}, {a:%H:%M}"


def group_facts(db, group: Group, now: Optional[datetime] = None) -> dict:
    """Everything the bot may say about this group, in the words it will say it.

    Nothing here is about a person: lessons, deadlines and recordings belong to the whole group.
    """
    now = now or _now()
    lessons = (db.query(Event)
               .join(EventGroup, EventGroup.event_id == Event.id)
               .filter(EventGroup.group_id == group.id,
                       Event.is_active.is_(True),
                       Event.event_type == "class",
                       Event.start_datetime >= now,
                       event_has_operational_group_clause())
               .order_by(Event.start_datetime)
               .limit(LESSONS_AHEAD)
               .all())

    homework = (db.query(Assignment)
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

    recordings = (db.query(Event, LessonRecording)
                  .join(EventGroup, EventGroup.event_id == Event.id)
                  .join(LessonRecording, LessonRecording.event_id == Event.id)
                  .filter(EventGroup.group_id == group.id,
                          LessonRecording.status == "ready",
                          LessonRecording.hls_url.isnot(None))
                  .order_by(Event.start_datetime.desc())
                  .limit(RECORDINGS_SHOWN)
                  .all())

    teacher = db.get(UserInDB, group.teacher_id) if group.teacher_id else None
    curator = db.get(UserInDB, group.curator_id) if group.curator_id else None
    return {
        "группа": group.name,
        "преподаватель": teacher.name if teacher else None,
        "куратор": curator.name if curator else None,
        "сейчас": _when(now, None),
        "ближайшие_уроки": [
            {"когда": _when(lesson.start_datetime, lesson.end_datetime),
             "тема": lesson.topic or None,
             "ссылка_meet": lesson.meeting_url or None}
            for lesson in lessons
        ],
        "домашние_задания": [
            {"название": task.title,
             "срок": _date(task.due_date) if task.due_date else "без срока"}
            for task in homework
        ],
        "записи_уроков": [
            {"урок": _when(event.start_datetime, None),
             "ссылка": lms_url(f"/recordings?watch={event.id}")}
            for event, _recording in recordings
        ],
    }


def _has_anything(facts: dict) -> bool:
    return any(facts[key] for key in ("ближайшие_уроки", "домашние_задания", "записи_уроков"))


# ── the answer ───────────────────────────────────────────────────────────────────────────

def is_courtesy(text: str) -> bool:
    """A thank-you / acknowledgement should not restart a group conversation."""
    return bool(_COURTESY.fullmatch(text or ""))


def capabilities_reply(question: str) -> str:
    """Describe the deliberately small, safe group-chat scope without an LLM."""
    text = (question or "").lower()
    if any(char in text for char in "әіңғқұүө"):
        return (
            "Топтың кестесі, үй тапсырмасы мен мерзімдері және сабақ жазбалары туралы "
            "көмектесе аламын. Жеке сұрақтар бойынша маған жеке жазыңыз."
        )
    if re.search(r"\b(?:what|can|you|do)\b", text):
        return (
            "I can help with this group's schedule, homework and deadlines, and lesson recordings. "
            "For personal questions, please message me privately."
        )
    return (
        "Могу помочь с расписанием группы, домашними заданиями и сроками, а также с записями уроков. "
        "По личным вопросам напишите мне в личные сообщения."
    )


def has_supported_topic(question: str) -> bool:
    """Whether the fact bundle can answer this question without guessing."""
    if _UNSUPPORTED_TOPIC.search(question or "") or _OUT_OF_SCOPE_REQUEST.search(question or ""):
        return False
    return bool(
        _SCHEDULE_TOPIC.search(question or "")
        or _HOMEWORK_TOPIC.search(question or "")
        or _RECORDING_TOPIC.search(question or "")
    )

def _plain_answer(facts: dict, question: str) -> Optional[str]:
    """The answer without a model: the part of the facts the question is about.

    Used when the model is unreachable or unconfigured — a bot that says "ближайший урок: …"
    is worth more than one that says nothing at all.
    """
    text = question or ""
    wants_homework = bool(_HOMEWORK_TOPIC.search(text))
    wants_recording = bool(_RECORDING_TOPIC.search(text))
    wants_schedule = bool(_SCHEDULE_TOPIC.search(text))
    if wants_homework and facts["домашние_задания"]:
        items = "; ".join(f"{t['название']} — до {t['срок']}" for t in facts["домашние_задания"][:3])
        return f"Домашние задания: {items}."
    if wants_recording and facts["записи_уроков"]:
        last = facts["записи_уроков"][0]
        return f"Запись последнего урока ({last['урок']}): {last['ссылка']} — нужен вход в LMS."
    if wants_schedule and facts["ближайшие_уроки"]:
        lesson = facts["ближайшие_уроки"][0]
        link = f" Ссылка: {lesson['ссылка_meet']}" if lesson["ссылка_meet"] else ""
        return f"Ближайший урок — {lesson['когда']} (время Алматы).{link}"
    return None


def _ask_model(facts: dict, question: str, key: str) -> tuple:
    """(answer, answered) from the model, or (None, False) when it cannot be reached."""
    import httpx

    payload = {
        "model": MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"ФАКТЫ:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
                                        f"ВОПРОС УЧЕНИКА:\n{question}"},
        ],
    }
    try:
        with httpx.Client(timeout=MODEL_TIMEOUT_SECONDS) as client:
            response = client.post(MODEL_URL, headers={"Authorization": f"Bearer {key}"}, json=payload)
        response.raise_for_status()
        body = json.loads(response.json()["choices"][0]["message"]["content"])
    except Exception as e:
        logger.warning("group bot: the model did not answer (%s)", str(e)[:200])
        return None, False
    answer = (body.get("answer") or "").strip()
    return (answer, True) if body.get("answered") and answer else (None, False)


def _notify_curator(db, group: Group, question: str, chat_title: Optional[str], row_id: int) -> None:
    if not group.curator_id:
        return
    db.add(Notification(
        user_id=group.curator_id,
        title=f"Вопрос в чате группы {group.name}",
        content=f"{chat_title or group.name}: «{question.strip()[:400]}»\n"
                "Бот не смог ответить по данным LMS — ответьте, пожалуйста, в чате группы.",
        notification_type="group_bot_question",
        related_id=row_id,
    ))


def answer(db, *, support_group_id: int, text: str, chat_title: Optional[str] = None,
           telegram_chat_id: Optional[int] = None, message_id: Optional[int] = None,
           asker: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    """One question from a group chat → what the bot should say there.

    Raises :class:`NotLinked` (unknown chat) or :class:`SwitchedOff` (bot off, or the group is
    outside the pilot) before anything is read or written.
    """
    now = now or _now()
    group = group_for(db, support_group_id)
    if not group_bot_settings.enabled_for(db, group):
        raise SwitchedOff(f"the group bot is off for group {group.id}")

    question = (text or "").strip()[:MAX_QUESTION_CHARS]
    asker = asker or {}
    row = TelegramGroupQuestion(
        lms_group_id=group.id, support_group_id=support_group_id, telegram_chat_id=telegram_chat_id,
        chat_title=chat_title, message_id=message_id,
        asker_telegram_id=asker.get("telegram_user_id"), asker_username=asker.get("username"),
        asker_name=asker.get("name"), question=question, created_at=now,
    )

    if is_personal(question):
        row.answer, row.private_hint, row.model = PRIVATE_REPLY, True, None
        db.add(row)
        db.commit()
        return {"answer": PRIVATE_REPLY, "private_hint": True, "handed_to_curator": False,
                "question_id": row.id}

    if is_courtesy(question):
        # It was addressed to us because it replied to a bot message, but it is
        # not a question.  Keep an audit row without posting or bothering the
        # curator, and let Support leave the chat silent.
        db.add(row)
        db.commit()
        return {"answer": None, "silent": True, "private_hint": False,
                "handed_to_curator": False, "question_id": row.id}

    if _CAPABILITIES.search(question):
        reply = capabilities_reply(question)
        row.answer, row.model = reply, "facts"
        db.add(row)
        db.commit()
        return {"answer": reply, "private_hint": False, "handed_to_curator": False,
                "question_id": row.id}

    facts = group_facts(db, group, now)
    reply, model_used = None, None
    # The model has no tools and only this fact bundle.  Do not ask it to turn
    # an unrelated sentence into a timetable answer: unanswered things go to
    # the curator, exactly as the group-chat policy promises.
    if has_supported_topic(question) and _has_anything(facts):
        key = model_key()
        if key:
            reply, answered = _ask_model(facts, question, key)
            model_used = MODEL if answered else None
        if reply is None:
            reply = _plain_answer(facts, question)
            model_used = "facts" if reply else None

    if reply is None:
        row.answer = CURATOR_REPLY if group.curator_id else NOTHING_YET
        row.handed_to_curator = bool(group.curator_id)
        db.add(row)
        db.flush()
        _notify_curator(db, group, question, chat_title, row.id)
        db.commit()
        return {"answer": row.answer, "private_hint": False,
                "handed_to_curator": row.handed_to_curator, "question_id": row.id}

    row.answer, row.model = reply, model_used
    db.add(row)
    db.commit()
    return {"answer": reply, "private_hint": False, "handed_to_curator": False, "question_id": row.id}


def recent_count(db, now: Optional[datetime] = None) -> int:
    """How many questions the bot has been asked this week — the switch panel's one number."""
    now = now or _now()
    return (db.query(TelegramGroupQuestion)
            .filter(TelegramGroupQuestion.created_at >= now - RECENT_WINDOW).count())
