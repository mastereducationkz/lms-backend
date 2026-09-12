"""Telling a group's Telegram chat that an approved lesson request moved, cancelled, or handed
a lesson to a substitute (owner, 2026-09-12).

The lesson-invitation job (:mod:`src.services.telegram_invitations`) says a lesson is starting;
this module says a lesson's facts just changed. Same split of duties as everywhere else: the
LMS decides *what* changed and *whether* to say so, Support carries the message.

**Queued, not sent, at the moment of approval.** :func:`queue_for_request` is called from inside
:mod:`src.lesson_requests.helpers` — the same transaction that moves the event, deactivates it,
or reassigns its teacher — so a notice row exists if and only if the change it describes was
actually committed. Sending happens on the next tick of the same minute-scheduler that drains
lesson invitations (:func:`send_due_notices`), which keeps a slow or unreachable Support from
ever blocking a request's approval.

**Every linked chat of the lesson, not only the requester's group.** An ``Event`` can carry
several groups (a shared lesson); moving or cancelling it moves or cancels all of them, so every
one of the event's linked chats gets its own row and its own message.

Off unless ``ENABLE_TELEGRAM_LESSON_CHANGE_NOTICES`` is set, exactly like the invitation job.
"""
import html
import logging
import os
from datetime import datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramGroupLink, TelegramLessonChangeNotice
from src.schemas.models import Event, EventGroup, Group, LessonRequest, UserInDB
from src.services import support_client
from src.services.telegram_invitations import RU_MONTHS, RU_WEEKDAYS, _almaty

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
SEND_TIMEOUT_SECONDS = 45
SYSTEM_ACTOR = "lms-lesson-notices@mastereducation.kz"

RESCHEDULED, CANCELLED, SUBSTITUTED = "rescheduled", "cancelled", "substituted"


def enabled() -> bool:
    return os.getenv("ENABLE_TELEGRAM_LESSON_CHANGE_NOTICES", "").strip().lower() in ("1", "true", "yes", "on")


def _when(value: datetime) -> str:
    a = _almaty(value)
    return f"{RU_WEEKDAYS[a.weekday()]}, {a.day} {RU_MONTHS[a.month - 1]}, {a:%H:%M} (время Алматы)"


def _lesson_label(title: str, group_names: Iterable[str]) -> str:
    """The same "group name, урок N" the invitation uses, so a chat sees one voice."""
    from src.services.telegram_invitations import _without_teacher

    import re
    match = re.match(r"^(.*?):\s*(Lesson\s+\d+.*)$", title, re.IGNORECASE)
    names = [n for n in group_names if n] or [match.group(1) if match else title]
    name = ", ".join(filter(None, (_without_teacher(n) for n in names))) or title
    number = re.sub(r"^Lesson\b", "урок", match.group(2).strip(), flags=re.IGNORECASE) if match else None
    return f"{name}, {number}" if number else name


def notice_text(change_type: str, label: str, *, old_start: Optional[datetime] = None,
                new_start: Optional[datetime] = None, meeting_url: Optional[str] = None,
                new_teacher_name: Optional[str] = None) -> str:
    """The message a group's chat sees. Never a reason (a teacher's own words are not the
    group's business); only the fact that changed."""
    if change_type == RESCHEDULED:
        lines = ["Урок перенесён", label,
                f"Было: {_when(old_start)}" if old_start else None,
                f"Стало: {_when(new_start)}" if new_start else None,
                f"Google Meet: {meeting_url}" if meeting_url else None]
    elif change_type == CANCELLED:
        lines = ["Урок отменён", label,
                f"{_when(old_start)}" if old_start else None]
    elif change_type == SUBSTITUTED:
        lines = ["Урок проведёт другой преподаватель", label,
                f"{_when(old_start)}" if old_start else None,
                f"Преподаватель: {new_teacher_name}" if new_teacher_name else None]
    else:
        raise ValueError(f"unknown change_type {change_type!r}")
    return html.escape("\n".join(line for line in lines if line), quote=False)


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def queue_for_request(db, lr: "LessonRequest", event: "Event", change_type: str, *,
                      old_start: Optional[datetime] = None, new_start: Optional[datetime] = None,
                      old_teacher_id: Optional[int] = None, new_teacher_id: Optional[int] = None) -> int:
    """Write one pending row per linked chat of ``event``'s groups. Never commits — the caller's
    transaction (the request's own approval) is what makes the notice real.

    Returns how many rows were queued. Safe to call more than once for the same request (a
    retried approval, a repair script): the unique constraint makes a second call a no-op per
    group, so nothing is ever said twice for one approved change.
    """
    if not enabled():
        return 0
    links = (db.query(EventGroup.group_id, TelegramGroupLink.support_group_id)
             .join(TelegramGroupLink, TelegramGroupLink.lms_group_id == EventGroup.group_id)
             .filter(EventGroup.event_id == event.id).all())
    if not links:
        return 0
    # Checked before inserting, not caught as an IntegrityError afterwards: this runs inside the
    # approval's own transaction, and a caller who calls twice (a retried approval, a repair
    # script) must get a clean no-op rather than a failed flush the caller then has to recover
    # from. The unique constraint stays as the backstop for a genuine concurrent double-approve.
    already = {group_id for (group_id,) in
              db.query(TelegramLessonChangeNotice.lms_group_id)
              .filter(TelegramLessonChangeNotice.lesson_request_id == lr.id,
                      TelegramLessonChangeNotice.lms_group_id.in_([g for g, _s in links]))}
    queued = 0
    for group_id, support_group_id in links:
        if group_id in already:
            continue
        row = TelegramLessonChangeNotice(
            lesson_request_id=lr.id, event_id=event.id, lms_group_id=group_id,
            support_group_id=support_group_id, change_type=change_type,
            old_start_datetime=_naive(old_start) if old_start else None,
            new_start_datetime=_naive(new_start) if new_start else None,
            old_teacher_id=old_teacher_id, new_teacher_id=new_teacher_id,
        )
        db.add(row)
        try:
            db.flush()
            queued += 1
        except IntegrityError:
            # A genuine race with another approval of the same request — the DB-level backstop.
            db.rollback()
    return queued


def due(db) -> list:
    """Every notice still worth trying: never sent, or failed with attempts left."""
    return (db.query(TelegramLessonChangeNotice)
            .filter(TelegramLessonChangeNotice.status.in_(("pending", "failed")),
                    TelegramLessonChangeNotice.attempts < MAX_ATTEMPTS)
            .order_by(TelegramLessonChangeNotice.created_at)
            .all())


def _claim(db, notice_id: int) -> bool:
    """True once, for the tick that actually gets to send this row."""
    updated = (db.query(TelegramLessonChangeNotice)
               .filter(TelegramLessonChangeNotice.id == notice_id,
                       TelegramLessonChangeNotice.status.in_(("pending", "failed")))
               .update({"attempts": TelegramLessonChangeNotice.attempts + 1}, synchronize_session=False))
    db.commit()
    return bool(updated)


def _text_for(db, notice: TelegramLessonChangeNotice) -> Optional[str]:
    group = db.get(Group, notice.lms_group_id)
    label = _lesson_label(group.name if group else "Урок", [group.name] if group else [])
    event = db.get(Event, notice.event_id) if notice.event_id else None
    new_teacher = db.get(UserInDB, notice.new_teacher_id) if notice.new_teacher_id else None
    return notice_text(
        notice.change_type, label,
        old_start=notice.old_start_datetime, new_start=notice.new_start_datetime,
        meeting_url=event.meeting_url if event and notice.change_type == RESCHEDULED else None,
        new_teacher_name=new_teacher.name if new_teacher else None,
    )


def send_due_notices(db, now: Optional[datetime] = None) -> dict:
    """Send every notice still due. Safe to call every minute from any number of places."""
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    if not enabled():
        return summary
    for notice in due(db):
        if not _claim(db, notice.id):
            continue
        text = _text_for(db, notice)
        if not text:
            notice.status, notice.error = "skipped", "lesson or group no longer exists"
            db.commit()
            summary["skipped"] += 1
            continue
        outcome = {}
        try:
            result = support_client.call(
                "POST", "/telegram/messages",
                actor_email=SYSTEM_ACTOR, actor_name="LMS lesson notices",
                json_body={
                    "telegram_group_id": notice.support_group_id,
                    "text": text,
                    "idempotency_key": f"lesson-change:{notice.lesson_request_id}:{notice.lms_group_id}",
                    "silent": False,
                    "disable_web_page_preview": True,
                },
                timeout=SEND_TIMEOUT_SECONDS,
            ) or {}
            outcome = {"status": "sent", "telegram_message_id": result.get("telegram_message_id"),
                      "sent_at": datetime.now(timezone.utc).replace(tzinfo=None), "error": None}
        except HTTPException as exc:
            detail = f"{exc.status_code}: {exc.detail}"
            if exc.status_code in (400, 404, 409, 422):
                outcome = {"status": "skipped", "error": detail}
            else:
                outcome = {"status": "failed", "error": detail}
        except Exception as e:
            outcome = {"status": "failed", "error": str(e)[:500]}

        for key, value in outcome.items():
            setattr(notice, key, value)
        if notice.status == "failed" and notice.attempts >= MAX_ATTEMPTS:
            logger.warning("lesson change notice %s: giving up after %s attempts: %s",
                           notice.id, notice.attempts, notice.error)
        db.commit()
        summary[outcome["status"]] = summary.get(outcome["status"], 0) + 1
    return summary
