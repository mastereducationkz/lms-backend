"""Every lesson starting in the reminder window is reminded — in one tick, once.

Until 2026-09-11 the scheduler pruned its in-memory "already sent" set by parsing timestamps
back out of the keys, comparing a naive one with an aware one. That raised on every tick, right
after the first lesson's reminders went out and *inside* the loop — so each tick reminded one
lesson and silently skipped every other lesson starting at the same time. At 19:00, when a
dozen groups start together, most of them were never reminded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.services import lesson_reminder_scheduler as lrs
from tests.test_lms_calendar_hides_stopped_groups import _Borrowed
from tests.test_operational_groups import db, world  # noqa: F401 - transactional fixtures


def _scheduler(world, monkeypatch, send):
    world["db"].execute(text("SET LOCAL TIME ZONE 'UTC'"))
    monkeypatch.setattr(lrs, "SessionLocal", lambda: _Borrowed(world["db"]))
    monkeypatch.setattr(lrs.LessonReminderScheduler, "_send_event_reminders", send)
    return lrs.LessonReminderScheduler()


def _lessons_at_the_same_time(world, n):
    lessons = []
    for i in range(n):
        group = world["group"](name=f"group {i}")
        world["enrol"](group)
        lessons.append(world["lesson"](group, days_ahead=30 / 1440))
    return lessons


def test_every_lesson_in_the_window_is_reminded_in_one_tick(world, monkeypatch):
    lessons = _lessons_at_the_same_time(world, 3)
    sent = []
    scheduler = _scheduler(world, monkeypatch, lambda self, db, event: sent.append(event.id) or True)

    scheduler._check_and_send_reminders()

    assert sorted(sent) == sorted(e.id for e in lessons)


def test_a_lesson_is_reminded_once(world, monkeypatch):
    _lessons_at_the_same_time(world, 2)
    sent = []
    scheduler = _scheduler(world, monkeypatch, lambda self, db, event: sent.append(event.id) or True)
    scheduler._check_and_send_reminders()
    scheduler._check_and_send_reminders()
    assert len(sent) == 2, "the second tick finds both already handled"


def test_one_lessons_failure_does_not_skip_the_others(world, monkeypatch):
    lessons = _lessons_at_the_same_time(world, 3)
    broken = lessons[0].id
    sent = []

    def send(self, db, event):
        if event.id == broken:
            raise RuntimeError("mail provider hiccup")
        sent.append(event.id)
        return True

    scheduler = _scheduler(world, monkeypatch, send)
    scheduler._check_and_send_reminders()
    assert sorted(sent) == sorted(e.id for e in lessons[1:])
    assert not any(str(broken) in key for key in scheduler.sent_reminders), "it is tried again next tick"


def test_a_day_old_entry_is_forgotten_and_a_fresh_one_kept(world, monkeypatch):
    scheduler = _scheduler(world, monkeypatch, lambda self, db, event: True)
    now = datetime.now(timezone.utc)
    scheduler.sent_reminders = {"event_1_old": now - timedelta(hours=25),
                                "post_lesson_2_2026-09-11T14:00:00": now - timedelta(hours=1)}
    scheduler._forget_old(now)
    assert list(scheduler.sent_reminders) == ["post_lesson_2_2026-09-11T14:00:00"]
