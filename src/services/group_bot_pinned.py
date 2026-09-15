"""The pinned timetable: one message per live chat, pinned silently and kept true (owner, 2026-09-15).

It carries the regular week, this week's changes, the next lesson with its link, the answer
buttons and — once the group has a calendar — «📆 Добавить в календарь». It is posted after the
hello (a minute later, so they arrive in order), and afterwards only *edited*: whenever what it
would say differs from what it says (a moved lesson, a new week, the next lesson changing), the
same message is rewritten, which notifies nobody.

Removal is respected: if someone unpins or deletes it, Support reports the message gone and the
row is closed for good — the bot never posts or pins it again. ``/schedule`` and the answer buttons
keep working regardless.

Off unless ``ENABLE_TELEGRAM_PINNED_TIMETABLE`` is set.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramPinnedTimetable
from src.schemas.models import Event
from src.services import group_bot, group_bot_hello, group_bot_keyboard as keyboard
from src.services import group_bot_outbox as outbox
from src.services import group_bot_render as render

logger = logging.getLogger(__name__)

FLAG = "ENABLE_TELEGRAM_PINNED_TIMETABLE"
AFTER_HELLO = timedelta(seconds=60)
CALENDAR_BUTTON = "📆 Добавить в календарь"


def enabled(db=None) -> bool:
    return outbox.flag(FLAG)


def content(db, group, now: datetime) -> tuple[str, dict]:
    """(HTML text, Telegram reply_markup) — exactly what the pinned message should show now."""
    upcoming = group_bot._lessons(db, group, now).filter(
        Event.start_datetime < now + group_bot.SCHEDULE_HORIZON).all()
    text = render.schedule_answer(group, upcoming, now, "ru")
    if upcoming:
        text += "\n\n" + render.next_line(upcoming[0], now, "ru")
    rows = keyboard.keyboard(group.id)
    links = keyboard.calendar_links(db, group)
    if links and links.get("google_url"):
        rows = rows + [[{"text": CALENDAR_BUTTON, "url": links["google_url"]}]]
    return text, keyboard.to_telegram(rows)


def content_hash(text: str, markup: dict) -> str:
    return hashlib.sha256(json.dumps([text, markup], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def run(db, live: list, budget: outbox.Budget, now: datetime) -> dict:
    summary = {"posted": 0, "edited": 0, "removed": 0, "failed": 0, "skipped": 0}
    for group, link in live:
        if not group_bot_hello.greeted(db, group.id, now, older_than=AFTER_HELLO):
            continue
        row = db.query(TelegramPinnedTimetable).filter(TelegramPinnedTimetable.lms_group_id == group.id).first()
        if row is not None and (row.removed_at is not None or row.status in ("removed", "skipped")):
            continue
        text, markup = content(db, group, now)
        digest = content_hash(text, markup)

        if row is None or row.telegram_message_id is None:
            if row is None:
                row = TelegramPinnedTimetable(lms_group_id=group.id, support_group_id=link.support_group_id,
                                              status="pending", attempts=0, created_at=now)
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    continue
            if row.attempts >= outbox.MAX_ATTEMPTS or not budget.take():
                continue
            row.attempts += 1
            db.commit()
            outcome = outbox.post(link.support_group_id, text, f"pinned-timetable:{group.id}",
                                  silent=True, pin=True, reply_markup=markup)
            if outcome["status"] == "sent":
                row.telegram_message_id, row.content_hash = outcome["telegram_message_id"], digest
                row.status, row.posted_at, row.updated_at, row.last_error = "posted", now, now, None
                summary["posted"] += 1
            else:
                row.status, row.last_error = outcome["status"], outcome["error"]
                summary[outcome["status"]] += 1
            db.commit()
            continue

        if row.content_hash == digest:
            continue
        if not budget.take():
            break
        result = outbox.edit(link.support_group_id, row.telegram_message_id, text, markup)
        if result.get("gone"):
            row.status, row.removed_at = "removed", now
            summary["removed"] += 1
        elif result.get("ok"):
            row.content_hash, row.updated_at, row.last_error = digest, now, None
            summary["edited"] += 1
        else:
            row.last_error = (result.get("description") or "edit failed")[:500]
            summary["failed"] += 1
        db.commit()
    return summary
