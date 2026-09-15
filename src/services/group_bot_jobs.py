"""One minute tick for everything the bot says in group chats on its own (owner, 2026-09-15).

Run from the lesson-reminder scheduler (the ``scheduler`` container). The live chats are worked out
once and shared; the Support call budget is shared too, so a first switch-on paces itself; each
job is isolated — one failing never stops the others. Every job has its own off-by-default flag:

* ``ENABLE_TELEGRAM_AUTO_HELLO`` (+ app setting ``group_bot.auto_hello_enabled``) — :mod:`group_bot_hello`
* ``ENABLE_TELEGRAM_PINNED_TIMETABLE`` — :mod:`group_bot_pinned`
* ``ENABLE_TELEGRAM_SCHEDULE_CHANGE_NOTICES`` — :mod:`group_bot_schedule_watch`
* ``ENABLE_TELEGRAM_DIGEST`` (+ ``group_bot.digest_enabled`` / ``digest_off_groups``) — :mod:`group_bot_digest`
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from src.config import SessionLocal
from src.services import group_bot_digest, group_bot_hello, group_bot_outbox as outbox
from src.services import group_bot_pinned, group_bot_schedule_watch

logger = logging.getLogger(__name__)

# The hello first: the pinned timetable waits for it.
JOBS = (
    ("hello", group_bot_hello),
    ("pinned_timetable", group_bot_pinned),
    ("schedule_change", group_bot_schedule_watch),
    ("digest", group_bot_digest),
)


def run_tick(db, now: Optional[datetime] = None) -> dict:
    active = [(name, job) for name, job in JOBS if job.enabled(db)]
    if not active:
        return {}
    now = now or outbox.utcnow()
    live = outbox.live_links(db, now)
    budget = outbox.Budget()
    summary = {}
    for name, job in active:
        try:
            summary[name] = job.run(db, live, budget, now)
        except Exception:
            db.rollback()
            logger.exception("group bot job %s failed", name)
    return summary


def run_all() -> dict:
    db = SessionLocal()
    try:
        return run_tick(db)
    finally:
        db.close()


def run_from_scheduler() -> None:
    """The lesson-reminder scheduler's minute hook: never raises, logs only when something happened."""
    try:
        summary = run_all()
        if any(any(part.values()) for part in summary.values()):
            logger.info("📨 [TELEGRAM] Group bot posts: %s", summary)
    except Exception:
        logger.exception("❌ [TELEGRAM] Group bot posts failed")
