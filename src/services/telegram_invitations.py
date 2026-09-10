"""Lesson invitations in each group's Telegram chat, five minutes before an LMS Meet lesson.

Owner decisions (2026-09-10): a plain post (no mentions — a bot cannot list a group's members,
and only students who linked their Telegram could be tagged), 5 minutes before the start, only
for lessons held in an LMS Google Meet room, in Russian — the same text as the lesson card's
"Скопировать приглашение" button (lms-front src/lib/meetLinks.ts, pinned by tests on both sides).

The LMS decides *what* and *when*; the Support platform's bot carries the message
(POST /service-api/telegram/messages, idempotent on our key). Off unless
ENABLE_TELEGRAM_LESSON_INVITES is set, and a group without a confirmed chat link is never sent to.
"""
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from src.announcements.models import TelegramGroupLink, TelegramLessonInvitation
from src.schemas.models import Event, EventGroup, Group, UserInDB
from src.services import support_client
from src.services.operational_groups import event_has_operational_group_clause

logger = logging.getLogger(__name__)

ALMATY = ZoneInfo("Asia/Almaty")
LEAD = timedelta(minutes=5)
# A tick missed by a restart still sends, but an invitation is pointless once the lesson is well
# under way.
GRACE_AFTER_START = timedelta(minutes=10)
MAX_ATTEMPTS = 3
SEND_TIMEOUT_SECONDS = 45
SYSTEM_ACTOR = "lms-lesson-invitations@mastereducation.kz"

RU_WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря")


def enabled() -> bool:
    return os.getenv("ENABLE_TELEGRAM_LESSON_INVITES", "").strip().lower() in ("1", "true", "yes", "on")


# --- the text ------------------------------------------------------------------------------


def _without_teacher(name: str) -> str:
    """A group name without its trailing " - Teacher" (teacher names are short)."""
    dash = name.rfind(" - ")
    return name[:dash].strip() if dash != -1 and len(name) - dash - 3 <= 24 else name.strip()


def _almaty(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ALMATY)


def invitation_text(title: str, group_names: Iterable[str], start: datetime, end: datetime,
                    meeting_url: str) -> str:
    """The invitation, as the lesson card's copy button writes it. HTML-escaped for Telegram."""
    match = re.match(r"^(.*?):\s*(Lesson\s+\d+.*)$", title, re.IGNORECASE)
    names = [n for n in group_names if n] or [match.group(1) if match else title]
    name = ", ".join(filter(None, (_without_teacher(n) for n in names))) or title
    number = re.sub(r"^Lesson\b", "урок", match.group(2).strip(), flags=re.IGNORECASE) if match else None
    s, e = _almaty(start), _almaty(end)
    day = f"{RU_WEEKDAYS[s.weekday()]}, {s.day} {RU_MONTHS[s.month - 1]}"
    lines = [
        "Приглашение на урок",
        f"{name}, {number}" if number else name,
        f"{day}, {s:%H:%M}–{e:%H:%M} (время Алматы)",
        f"Google Meet: {meeting_url}",
        "Подключайтесь за пару минут до начала.",
    ]
    return html.escape("\n".join(lines), quote=False)


# --- suggesting which chat is which group's ------------------------------------------------

_MONTHS_EN = ("january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december")
_RU_STEMS = (("январ", 0), ("феврал", 1), ("март", 2), ("апрел", 3), ("мая", 4), ("май", 4),
             ("июн", 5), ("июл", 6), ("август", 7), ("сентябр", 8), ("октябр", 9),
             ("ноябр", 10), ("декабр", 11))
_NOISE = {"master", "education", "mastereducation", "masteredu", "group", "группа", "chat",
          "чат", "official", "class", "lessons", "урок", "уроки", "the"}


# "07.08 SAT August 6 2026": chat titles often lead with a dd.mm start date. Read as numbers it
# would contradict the group's own number ("August 6"), so dates like that are set aside.
_DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b")


def _tokens(text: str) -> set:
    tokens = set()
    for raw in re.split(r"[^\w]+", _DATE.sub(" ", (text or "").lower())):
        if not raw or raw in _NOISE:
            continue
        if raw.isdigit() and len(raw) <= 2:
            raw = str(int(raw))  # "06" is 6
        month = next((m for m in _MONTHS_EN if raw == m or (len(raw) >= 3 and m.startswith(raw))), None)
        if month is None:
            month = next((_MONTHS_EN[i] for stem, i in _RU_STEMS if raw.startswith(stem)), None)
        tokens.add(month or raw)
    return tokens


def _day_numbers(tokens: set) -> set:
    return {t for t in tokens if t.isdigit() and len(t) <= 2}


def match_score(lms_name: str, chat_title: str) -> float:
    """0..1: how likely a Telegram chat title names this LMS group.

    Words match in any order and in either language for months ("8 июля" = "July 8"); a day
    number that disagrees is decisive — "July 8 SAT" is never "July 18 SAT".
    """
    a, b = _tokens(lms_name), _tokens(chat_title)
    if not a or not b:
        return 0.0
    days_a, days_b = _day_numbers(a), _day_numbers(b)
    if days_a and days_b and days_a != days_b:
        return 0.0
    shared = len(a & b)
    return 0.5 * shared / len(a | b) + 0.5 * shared / min(len(a), len(b))


SUGGESTION_THRESHOLD = 0.6


def suggest_links(groups: list, chats: list, taken_groups: set = frozenset(),
                  taken_chats: set = frozenset()) -> dict:
    """{lms_group_id: (chat_id, chat_title, score)} — each chat suggested for at most one group.

    `groups` are (id, name), `chats` (id, title). Greedy on the best score overall, so the
    clearest match wins a contested chat.
    """
    pairs = []
    for gid, gname in groups:
        if gid in taken_groups:
            continue
        for cid, ctitle in chats:
            if cid in taken_chats:
                continue
            score = match_score(gname, ctitle)
            if score >= SUGGESTION_THRESHOLD:
                pairs.append((score, gid, cid, ctitle))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    chosen, used_groups, used_chats = {}, set(), set()
    for score, gid, cid, ctitle in pairs:
        if gid in used_groups or cid in used_chats:
            continue
        chosen[gid] = (cid, ctitle, round(score, 2))
        used_groups.add(gid)
        used_chats.add(cid)
    return chosen


# --- the job -------------------------------------------------------------------------------


def _held_in_an_lms_meet_room():
    """The lesson has one of our Meet rooms: an onboarded teacher teaches it, or owns its group
    (a substitute keeps the room the lesson was given)."""
    teacher, owner, link, group = aliased(UserInDB), aliased(UserInDB), aliased(EventGroup), aliased(Group)
    return and_(
        Event.meeting_url.like("https://meet.google.com/%"),
        or_(
            exists().where(and_(teacher.id == Event.teacher_id, teacher.workspace_email.isnot(None))).correlate(Event),
            exists().where(and_(link.event_id == Event.id, link.group_id == group.id,
                                owner.id == group.teacher_id, owner.workspace_email.isnot(None))).correlate(Event),
        ),
    )


def due(db, now: datetime) -> list:
    """(event, link) pairs whose invitation should go out now."""
    invitation = aliased(TelegramLessonInvitation)
    return (
        db.query(Event, TelegramGroupLink)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .join(TelegramGroupLink, TelegramGroupLink.lms_group_id == EventGroup.group_id)
        .outerjoin(invitation, and_(invitation.event_id == Event.id,
                                    invitation.lms_group_id == EventGroup.group_id))
        .filter(
            Event.event_type == "class",
            Event.is_active.is_(True),
            Event.start_datetime <= now + LEAD,
            Event.start_datetime > now - GRACE_AFTER_START,
            _held_in_an_lms_meet_room(),
            event_has_operational_group_clause(),
            or_(invitation.id.is_(None),
                and_(invitation.status.in_(("pending", "failed")), invitation.attempts < MAX_ATTEMPTS)),
        )
        .order_by(Event.start_datetime, Event.id)
        .all()
    )


def _claim(db, event_id: int, lms_group_id: int, support_group_id: int) -> Optional[int]:
    """The invitation row's id, created before sending; None if it is not ours to send."""
    row = (db.query(TelegramLessonInvitation)
           .filter_by(event_id=event_id, lms_group_id=lms_group_id)
           .first())
    if row is None:
        row = TelegramLessonInvitation(event_id=event_id, lms_group_id=lms_group_id,
                                       support_group_id=support_group_id, status="pending")
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # another tick created it first
            return None
    elif row.status in ("sent", "skipped") or row.attempts >= MAX_ATTEMPTS:
        return None
    row.attempts += 1
    rid = row.id
    db.commit()  # no transaction stays open across the network call
    return rid


def send_due_invitations(db, now: Optional[datetime] = None) -> dict:
    """Send every invitation that is due. Safe to call every minute from any number of places."""
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    if not enabled():
        return summary
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    for event, link in due(db, now):
        names = [n for (n,) in db.query(Group.name).join(EventGroup, EventGroup.group_id == Group.id)
                 .filter(EventGroup.event_id == event.id).order_by(Group.name).all()]
        text = invitation_text(event.title, names, event.start_datetime, event.end_datetime, event.meeting_url)
        event_id, lms_group_id, support_group_id = event.id, link.lms_group_id, link.support_group_id

        invitation_id = _claim(db, event_id, lms_group_id, support_group_id)
        if invitation_id is None:
            continue
        outcome = {}
        try:
            result = support_client.call(
                "POST", "/telegram/messages",
                actor_email=SYSTEM_ACTOR, actor_name="LMS lesson invitations",
                json_body={
                    "telegram_group_id": support_group_id,
                    "text": text,
                    "idempotency_key": f"lesson-invite:{event_id}:{lms_group_id}",
                    "silent": False,
                    "disable_web_page_preview": True,
                },
                # Support waits out a short Telegram 429 inside the request (worst case ~35 s).
                # A timeout earlier than that is still safe — the retry reuses the key — but
                # it would spend an attempt for nothing.
                timeout=SEND_TIMEOUT_SECONDS,
            ) or {}
            outcome = {"status": "sent", "telegram_message_id": result.get("telegram_message_id"),
                       "sent_at": datetime.now(timezone.utc).replace(tzinfo=None), "error": None}
        except HTTPException as exc:
            detail = f"{exc.status_code}: {exc.detail}"
            if exc.status_code in (400, 404, 409, 422):
                outcome = {"status": "skipped", "error": detail}  # chat unknown or not approved
            elif exc.status_code == 502 and exc.detail != support_client.UNREACHABLE_DETAIL:
                outcome = {"status": "failed", "error": detail, "attempts": MAX_ATTEMPTS}  # Telegram refused for good
            else:
                outcome = {"status": "failed", "error": detail}  # transient: next tick retries
        row = db.get(TelegramLessonInvitation, invitation_id)
        for key, value in outcome.items():
            setattr(row, key, value)
        db.commit()
        summary["sent" if outcome["status"] == "sent" else outcome["status"]] += 1
        log = logger.info if outcome["status"] == "sent" else logger.warning
        log("lesson %s → chat %s: %s %s", event_id, support_group_id, outcome["status"], outcome.get("error") or "")
    return summary
