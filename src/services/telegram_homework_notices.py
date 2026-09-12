"""Telling a group's Telegram chat that new homework was published, with a link to open it
(owner, 2026-09-12).

Same split of duties as the lesson-change notices: the LMS decides *what* changed and
*whether* to say so, Support carries the message. Queued right after
:func:`src.assignments.routes.assignments.create_assignment` commits — best effort, the same
convention that endpoint already uses for the email notification beside it, not the stricter
same-transaction guarantee the lesson-request flow gives :mod:`telegram_lesson_notices`.

**A real hyperlink, not a pasted URL.** Support's sanitizer keeps ``<a href>`` through exactly
like an announcement's toolbar link (:mod:`announcements.formatting` on the Support side), so
the free-text lines are escaped but the anchor tag itself is built, not escaped.

Off unless ``ENABLE_TELEGRAM_HOMEWORK_NOTICES`` is set, exactly like the sibling jobs.
"""
import html
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramGroupLink, TelegramHomeworkNotice
from src.schemas.models import Assignment, Group
from src.services import support_client
from src.services.recording_watch_links import lms_url
from src.services.telegram_invitations import RU_MONTHS, RU_WEEKDAYS, _almaty

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
SEND_TIMEOUT_SECONDS = 45
SYSTEM_ACTOR = "lms-homework-notices@mastereducation.kz"


def enabled() -> bool:
    return os.getenv("ENABLE_TELEGRAM_HOMEWORK_NOTICES", "").strip().lower() in ("1", "true", "yes", "on")


def _due_str(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    a = _almaty(value)
    return f"{RU_WEEKDAYS[a.weekday()]}, {a.day} {RU_MONTHS[a.month - 1]}, {a:%H:%M} (время Алматы)"


def notice_text(title: str, group_name: str, due_date: Optional[datetime], link: str) -> str:
    """The message a group's chat sees. Every free-text line is escaped on its own; the final
    anchor tag is appended raw so it survives Support's sanitizer as a real link."""
    lines = ["Новое домашнее задание", html.escape(group_name, quote=False), html.escape(title, quote=False)]
    due_str = _due_str(due_date)
    if due_str:
        lines.append(f"Срок: {html.escape(due_str, quote=False)}")
    body = "\n".join(lines)
    return f'{body}\n<a href="{html.escape(link, quote=True)}">Открыть задание</a>'


def queue_for_assignment(db, assignment: "Assignment", group: Optional["Group"]) -> int:
    """Write one pending row for ``group``'s linked chat, if it has one. Never commits — the
    caller decides when (see the module docstring on the atomicity trade-off here).

    Safe to call more than once for the same assignment: the unique constraint makes a
    second call a no-op, so nothing is ever said twice for one published assignment.
    """
    if not enabled() or group is None:
        return 0
    link_row = db.query(TelegramGroupLink).filter(TelegramGroupLink.lms_group_id == group.id).first()
    if not link_row:
        return 0
    already = db.query(TelegramHomeworkNotice.id).filter(
        TelegramHomeworkNotice.assignment_id == assignment.id,
        TelegramHomeworkNotice.lms_group_id == group.id,
    ).first()
    if already:
        return 0
    row = TelegramHomeworkNotice(
        assignment_id=assignment.id, lms_group_id=group.id, support_group_id=link_row.support_group_id,
    )
    db.add(row)
    try:
        db.flush()
        return 1
    except IntegrityError:
        # A genuine race with another call for the same assignment — the DB-level backstop.
        db.rollback()
        return 0


def due(db) -> list:
    """Every notice still worth trying: never sent, or failed with attempts left."""
    return (db.query(TelegramHomeworkNotice)
            .filter(TelegramHomeworkNotice.status.in_(("pending", "failed")),
                    TelegramHomeworkNotice.attempts < MAX_ATTEMPTS)
            .order_by(TelegramHomeworkNotice.created_at)
            .all())


def _claim(db, notice_id: int) -> bool:
    """True once, for the tick that actually gets to send this row."""
    updated = (db.query(TelegramHomeworkNotice)
               .filter(TelegramHomeworkNotice.id == notice_id,
                       TelegramHomeworkNotice.status.in_(("pending", "failed")))
               .update({"attempts": TelegramHomeworkNotice.attempts + 1}, synchronize_session=False))
    db.commit()
    return bool(updated)


def _text_for(db, notice: TelegramHomeworkNotice) -> Optional[str]:
    assignment = db.get(Assignment, notice.assignment_id)
    group = db.get(Group, notice.lms_group_id)
    if not assignment or not group:
        return None
    return notice_text(assignment.title, group.name, assignment.due_date, lms_url(f"/homework/{assignment.id}"))


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
            notice.status, notice.error = "skipped", "assignment or group no longer exists"
            db.commit()
            summary["skipped"] += 1
            continue
        outcome = {}
        try:
            result = support_client.call(
                "POST", "/telegram/messages",
                actor_email=SYSTEM_ACTOR, actor_name="LMS homework notices",
                json_body={
                    "telegram_group_id": notice.support_group_id,
                    "text": text,
                    "idempotency_key": f"homework:{notice.assignment_id}:{notice.lms_group_id}",
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
            logger.warning("homework notice %s: giving up after %s attempts: %s",
                           notice.id, notice.attempts, notice.error)
        db.commit()
        summary[outcome["status"]] = summary.get(outcome["status"], 0) + 1
    return summary
