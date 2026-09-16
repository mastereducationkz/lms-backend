"""Keep the pinned timetable in the bar at the top of the chat (owner, 2026-09-16).

Telegram shows the newest pinned message *by send date* in that bar. The timetable is pinned once
and afterwards only edited, so the first announcement pinned after it (NU FEST, 16 September) took
the bar — and re-pinning an older message never takes it back. The only way back on top is to be
newer: post a fresh copy, pin it silently, and delete the old one.

**When.** Support reports every newer pin — a person's pin as it happens, the composer's own right
after delivery — and :func:`note_pin` makes the chat due in :data:`REPORT_DELAY`, so a burst of
pins costs one re-post. As a safety net every chat's top pin is also read every
:data:`CHECK_EVERY`.

**A check** reads the message in the bar (``getChat``):

* the timetable → nothing to do;
* a newer message (or Support reported one) → re-post. First the old copy is edited to what it
  should say — ``gone`` means someone deleted the timetable, which is respected for good. Then
  the new copy is posted and pinned, and the old one deleted; when Telegram refuses to delete it,
  it is unpinned and turned into a one-line pointer instead;
* an older message, or nothing → someone unpinned the timetable. That is respected like a
  deletion, once a second read at least :data:`CONFIRM_UNPIN` later agrees (a read can lag).

Every step is saved before the next Support call, so a crashed tick resumes where it stopped; the
new copy is posted under an idempotency key of its own per re-post.

Off unless ``ENABLE_TELEGRAM_PINNED_ON_TOP`` is set, with the pinned timetable itself on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from src.announcements.models import TelegramPinnedTimetable
from src.services import group_bot_outbox as outbox
from src.services import group_bot_pinned as pinned

logger = logging.getLogger(__name__)

FLAG = "ENABLE_TELEGRAM_PINNED_ON_TOP"
REPORT_DELAY = timedelta(seconds=60)
CHECK_EVERY = timedelta(minutes=30)
CONFIRM_UNPIN = timedelta(minutes=15)
RETRY_AFTER = timedelta(minutes=5)
#: After a re-post that could not be made (no pin rights, the chat refusing messages) — try again tomorrow,
#: not every half hour: each failed try posts a copy and takes it straight back down.
GIVE_UP_FOR = timedelta(hours=24)
ROUTINE_CHECKS_PER_TICK = 4
#: A retired copy the chat would not let the bot delete: no buttons, no «!» (owner).
STALE_TEXT = "🗓 Это расписание устарело — актуальное всегда в закреплённом сообщении 📌"


def enabled(db=None) -> bool:
    return outbox.flag(FLAG) and pinned.enabled(db)


def _active(query):
    return query.filter(TelegramPinnedTimetable.status == "posted",
                        TelegramPinnedTimetable.removed_at.is_(None),
                        TelegramPinnedTimetable.telegram_message_id.isnot(None))


def note_pin(db, *, support_group_id: int, message_id: int, now: Optional[datetime] = None) -> bool:
    """Support saw ``message_id`` pinned in a chat → whether the timetable there needs a check."""
    now = now or outbox.utcnow()
    due = False
    rows = _active(db.query(TelegramPinnedTimetable)).filter(
        TelegramPinnedTimetable.support_group_id == support_group_id).all()
    for row in rows:
        if message_id <= row.telegram_message_id:
            continue                                   # an older message: the timetable stays on top
        row.reported_pin_id = max(row.reported_pin_id or 0, message_id)
        if row.check_due_at is None or row.check_due_at > now + REPORT_DELAY:
            row.check_due_at = now + REPORT_DELAY
        due = True
    db.commit()
    return due


def run(db, live: list, budget: outbox.Budget, now: datetime) -> dict:
    summary = {"checked": 0, "raised": 0, "unpinned": 0, "removed": 0, "failed": 0}
    by_group = {group.id: (group, link) for group, link in live}
    if not by_group:
        return summary
    rows = _active(db.query(TelegramPinnedTimetable)).filter(
        TelegramPinnedTimetable.lms_group_id.in_(list(by_group))).all()
    raising = [row for row in rows if row.raising_from_id is not None]
    due = [row for row in rows if row.raising_from_id is None and row.check_due_at is not None
           and row.check_due_at <= now]
    routine = sorted((row for row in rows if row.raising_from_id is None and row.check_due_at is None
                      and (row.pin_checked_at is None or row.pin_checked_at <= now - CHECK_EVERY)),
                     key=lambda row: row.pin_checked_at or datetime.min)[:ROUTINE_CHECKS_PER_TICK]

    for row in raising + due + routine:
        group, link = by_group[row.lms_group_id]
        if row.raising_from_id is None:
            if budget.left < 2:                        # a check that finds work wants a probe too
                break
            _check(db, row, group, link, budget, now, summary)
        if row.raising_from_id is not None and row.status == "posted":
            if not _raise(db, row, group, link, budget, now, summary):
                break
    return summary


def _check(db, row, group, link, budget, now, summary) -> None:
    budget.take()
    summary["checked"] += 1
    result = outbox.top_pinned(link.support_group_id)
    row.pin_checked_at = now
    if not result.get("ok"):
        row.last_error = (result.get("description") or "top pin unknown")[:500]
        transient = result.get("status_code") not in (404, 409)
        row.check_due_at = now + RETRY_AFTER if row.reported_pin_id and transient else None
        summary["failed"] += 1
        db.commit()
        return

    top, ours = result.get("pinned_message_id"), row.telegram_message_id
    row.check_due_at = None
    if top == ours:
        row.unpinned_seen_at, row.reported_pin_id = None, None
    elif (top is not None and top > ours) or (row.reported_pin_id or 0) > ours:
        row.unpinned_seen_at = None
        _start_raise(db, row, group, link, budget, now, summary)
        return
    elif row.unpinned_seen_at is None:
        row.unpinned_seen_at, row.check_due_at = now, now + CONFIRM_UNPIN
    elif now - row.unpinned_seen_at >= CONFIRM_UNPIN:
        row.status, row.removed_at = "removed", now
        summary["unpinned"] += 1
        logger.info("group %s: the pinned timetable was unpinned — respected", group.id)
    else:
        row.check_due_at = row.unpinned_seen_at + CONFIRM_UNPIN
    db.commit()


def _start_raise(db, row, group, link, budget, now, summary) -> None:
    text, markup = pinned.content(db, group, now)
    budget.take()
    probe = outbox.edit(link.support_group_id, row.telegram_message_id, text, markup)
    if probe.get("gone"):
        row.status, row.removed_at = "removed", now
        summary["removed"] += 1
    elif not probe.get("ok"):
        row.last_error = (probe.get("description") or "edit failed")[:500]
        row.check_due_at = now + RETRY_AFTER if probe.get("status_code") not in (404, 409) else None
        summary["failed"] += 1
    else:
        row.content_hash, row.updated_at = pinned.content_hash(text, markup), now
        row.raising_from_id, row.raises, row.raise_attempts = row.telegram_message_id, (row.raises or 0) + 1, 0
    db.commit()


def _raise(db, row, group, link, budget, now, summary) -> bool:
    """Post the new copy, then retire the old one → False when the tick's budget ran out."""
    old = row.raising_from_id
    if row.telegram_message_id == old:
        if row.raise_attempts >= outbox.MAX_ATTEMPTS:
            _finish(row, now, error="re-posting on top failed; kept the old copy", retry_at=now + GIVE_UP_FOR)
            summary["failed"] += 1
            db.commit()
            return True
        if not budget.take():
            return False
        text, markup = pinned.content(db, group, now)
        row.raise_attempts += 1
        db.commit()
        outcome = outbox.post(link.support_group_id, text, f"pinned-timetable:{group.id}:{row.raises}",
                              silent=True, pin=True, reply_markup=markup)
        if outcome["status"] != "sent":
            row.last_error = outcome["error"]
            if outcome["status"] == "skipped":
                _finish(row, now, error=outcome["error"], retry_at=now + GIVE_UP_FOR)
            summary["failed"] += 1
            db.commit()
            return True
        if outcome.get("pin_error"):
            # Posted but Telegram refused the pin: the old copy stays the pinned one.
            if budget.take():
                outbox.delete(link.support_group_id, outcome["telegram_message_id"])
            _finish(row, now, error=f"pin refused: {outcome['pin_error']}"[:500], retry_at=now + GIVE_UP_FOR)
            summary["failed"] += 1
            db.commit()
            return True
        row.telegram_message_id = outcome["telegram_message_id"]
        row.content_hash, row.updated_at, row.last_error = pinned.content_hash(text, markup), now, None
        db.commit()

    if budget.left < 3:                                # delete, or unpin + edit
        return False
    budget.take()
    deleted = outbox.delete(link.support_group_id, old)
    if not (deleted.get("ok") or deleted.get("gone")) and (
            deleted.get("status_code") != 200 or deleted.get("error_code") == 429):
        row.last_error = (deleted.get("description") or "delete failed")[:500]
        db.commit()
        return True                                    # Support or Telegram busy: retire it next tick
    if not (deleted.get("ok") or deleted.get("gone")):
        # Telegram does not let every old message go: unpin it and leave a pointer instead.
        budget.take()
        outbox.unpin(link.support_group_id, old)
        budget.take()
        outbox.edit(link.support_group_id, old, STALE_TEXT, None)
        logger.info("group %s: old timetable %s kept as a pointer (%s)", group.id, old, deleted.get("description"))
    _finish(row, now)
    summary["raised"] += 1
    db.commit()
    return True


def _finish(row, now: datetime, *, error: Optional[str] = None, retry_at: Optional[datetime] = None) -> None:
    row.raising_from_id, row.unpinned_seen_at, row.pin_checked_at = None, None, now
    if (row.reported_pin_id or 0) <= row.telegram_message_id:
        row.reported_pin_id, row.check_due_at = None, None   # else a pin newer than the new copy is still due
    if retry_at is not None:
        row.check_due_at = retry_at
    if error:
        row.last_error = error
