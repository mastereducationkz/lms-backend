"""The bot says hello once, the first time a group's chat goes live (owner, 2026-09-15).

Live is :func:`group_bot_settings.is_live` — the group's own teacher is connected to Workspace and
not suspended, the chat is linked, lessons are ahead — so the hello arrives together with the
things it promises (Meet rooms, recordings, the invitation five minutes before class). It catches
new groups, new chats and teachers connected later, and never a chat greeted before: staff's
manual hello announcements are recorded as ``source="backfill"`` greetings.

The texts are the owner's (:mod:`group_bot_hello_texts`): announcement #7 for a group, a
gender-neutral rewrite of the one-to-one #8 for an individual chat. The job stays off until the
owner approves that rewrite: ``ENABLE_TELEGRAM_AUTO_HELLO`` *and* ``group_bot.auto_hello_enabled``.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramGroupGreeting
from src.services import group_bot_hello_texts as texts
from src.services import group_bot_outbox as outbox
from src.services import group_bot_settings

logger = logging.getLogger(__name__)

FLAG = "ENABLE_TELEGRAM_AUTO_HELLO"
GROUP, INDIVIDUAL = "group", "individual"
_OPEN = ("pending", "failed")


def enabled(db) -> bool:
    return outbox.flag(FLAG) and group_bot_settings.auto_hello_enabled(db)


def variant_for(group) -> str:
    return INDIVIDUAL if (group.group_type or "").strip().lower() == INDIVIDUAL else GROUP


def text_for(variant: str) -> str:
    return texts.INDIVIDUAL_HELLO if variant == INDIVIDUAL else texts.GROUP_HELLO


def greeted(db, group_id: int, now: datetime, *, older_than) -> bool:
    """Whether the chat has been greeted at least ``older_than`` ago — the pinned timetable
    waits for the hello so the two never arrive in the wrong order."""
    row = (db.query(TelegramGroupGreeting)
           .filter(TelegramGroupGreeting.lms_group_id == group_id, TelegramGroupGreeting.status == "sent")
           .first())
    return bool(row and row.sent_at and row.sent_at <= now - older_than)


def run(db, live: list, budget: outbox.Budget, now: datetime) -> dict:
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    for group, link in live:
        row = db.query(TelegramGroupGreeting).filter(TelegramGroupGreeting.lms_group_id == group.id).first()
        if row is None:
            row = TelegramGroupGreeting(lms_group_id=group.id, support_group_id=link.support_group_id,
                                        variant=variant_for(group), source="auto", status="pending",
                                        attempts=0, created_at=now)
            db.add(row)
            try:
                db.commit()
            except IntegrityError:      # another tick greeted it first
                db.rollback()
                continue
        if row.status not in _OPEN or row.attempts >= outbox.MAX_ATTEMPTS:
            continue
        if not budget.take():
            break
        claimed = (db.query(TelegramGroupGreeting)
                   .filter(TelegramGroupGreeting.id == row.id, TelegramGroupGreeting.status.in_(_OPEN))
                   .update({"attempts": TelegramGroupGreeting.attempts + 1}, synchronize_session=False))
        db.commit()
        if not claimed:
            continue
        db.refresh(row)
        outcome = outbox.post(link.support_group_id, text_for(row.variant), f"hello:{group.id}", silent=False)
        row.status, row.error = outcome["status"], outcome["error"]
        if outcome["status"] == "sent":
            row.telegram_message_id, row.sent_at = outcome["telegram_message_id"], now
        elif outcome["status"] == "failed" and row.attempts >= outbox.MAX_ATTEMPTS:
            logger.warning("group bot hello for group %s: giving up: %s", group.id, row.error)
        db.commit()
        summary[outcome["status"]] += 1
    return summary
