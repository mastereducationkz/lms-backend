"""«⚠️ Расписание поменялось!» — when a group's regular week changes (owner, 2026-09-15).

The regular week is ``groups.schedule_config.schedule_items`` — what CRM «Регулярные уроки» and the
LMS schedule tools write, directly into that column, from five different places. Rather than hook
each writer, the job looks at the result: once a minute it compares every live group's regular
week with the one the chat was last told about.

* First sight of a group: remember its week, say nothing.
* A different week: hold it as *pending*. Admins edit the slots one at a time, so it has to stay
  unchanged for 15 minutes before one notice goes out with the final before → after. Edited back
  to the original meanwhile: nothing is said at all.
* Quiet hours (23:00–08:00 Almaty): the notice waits for the morning.

It reads the plan (``schedule_config``), not the week inferred from the calendar: an inferred
week drifts as lessons pass and the course runs out, and would announce changes nobody made. No
effective dates are stated (owner): the pinned timetable and ``/schedule`` carry the details.

Off unless ``ENABLE_TELEGRAM_SCHEDULE_CHANGE_NOTICES`` is set.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, time, timedelta

from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramScheduleWatch
from src.services import group_bot_outbox as outbox
from src.services import group_bot_render as render

logger = logging.getLogger(__name__)

FLAG = "ENABLE_TELEGRAM_SCHEDULE_CHANGE_NOTICES"
SETTLE = timedelta(minutes=15)


def enabled(db=None) -> bool:
    return outbox.flag(FLAG)


def pattern_of(group) -> str:
    """The regular week as stable JSON: [[weekday, "HH:MM", minutes], …], sorted."""
    return json.dumps([[slot.weekday, slot.start.strftime("%H:%M"), slot.minutes]
                       for slot in render.config_slots(group.schedule_config)])


def slots_of(pattern: str) -> list:
    return [render.Slot(int(day), datetime.strptime(at, "%H:%M").time(), int(minutes))
            for day, at, minutes in json.loads(pattern or "[]")]


def notice_text(group, old: str, new: str) -> str:
    lines = [render.header(group.name), "⚠️ Расписание поменялось!", "", "Было:",
             *render.pattern_lines(slots_of(old), "ru"), "", "Стало:",
             *render.pattern_lines(slots_of(new), "ru"), "",
             "Актуальное расписание всегда в закреплённом сообщении и по /schedule"]
    return "\n".join(lines)


def run(db, live: list, budget: outbox.Budget, now: datetime) -> dict:
    summary = {"sent": 0, "failed": 0, "skipped": 0, "watching": 0}
    for group, link in live:
        new = pattern_of(group)
        if new == "[]":
            continue
        row = db.query(TelegramScheduleWatch).filter(TelegramScheduleWatch.lms_group_id == group.id).first()
        if row is None:
            db.add(TelegramScheduleWatch(lms_group_id=group.id, pattern_json=new, attempts=0,
                                         created_at=now, updated_at=now))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            continue
        if new == row.pattern_json:
            if row.pending_json is not None:
                row.pending_json, row.pending_since, row.attempts, row.updated_at = None, None, 0, now
                db.commit()
            continue
        if row.pending_json != new:
            row.pending_json, row.pending_since, row.attempts, row.updated_at = new, now, 0, now
            db.commit()
            summary["watching"] += 1
            continue
        if now - row.pending_since < SETTLE or outbox.in_quiet_hours(now):
            continue
        if row.attempts >= outbox.MAX_ATTEMPTS:
            # Support kept failing: accept the new week so the job does not retry forever.
            row.pattern_json, row.pending_json, row.pending_since, row.updated_at = new, None, None, now
            db.commit()
            continue
        if not budget.take():
            break
        row.attempts += 1
        db.commit()
        key = f"schedule-change:{group.id}:{hashlib.sha1(new.encode()).hexdigest()[:16]}"
        outcome = outbox.post(link.support_group_id, notice_text(group, row.pattern_json, new), key, silent=False)
        if outcome["status"] in ("sent", "skipped"):
            if outcome["status"] == "sent":
                row.notified_at = now
            row.pattern_json, row.pending_json, row.pending_since, row.attempts = new, None, None, 0
        row.last_error, row.updated_at = outcome["error"], now
        db.commit()
        summary[outcome["status"]] += 1
    return summary
