"""What a tap on 🗓 / 📅 / 📝 shows: a Telegram alert only the person who tapped sees.

Telegram caps an alert at 200 characters of plain text — no bold, no tappable links, but line breaks
are kept, so each item gets its own line under a short title (owner, 2026-09-15) — so these are
the answers' facts squeezed onto a card, never the answers cut in half: whole items are added while
they fit, and what did not fit is counted («+ ещё 2 — /homework»). Length is measured in UTF-16 code
units, Telegram's own unit, so an emoji-heavy title can never push the text over the limit.
Russian only; the same privacy as every other answer (group facts, never a person).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from src.schemas.models import Event
from src.services import group_bot, group_bot_render as render, group_bot_settings

LIMIT = 200
TITLE_CHARS = 40
ACTIONS = ("schedule", "lessons", "homework")


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def fits(text: str) -> bool:
    return units(text) <= LIMIT


def cap(text: str) -> str:
    """Last line of defence: shorten by whole code points (never half a surrogate pair)."""
    while not fits(text):
        text = text[:-2].rstrip() + "…"
    return text


def short(title: Optional[str], limit: int = TITLE_CHARS) -> str:
    title = " ".join((title or "").split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def _greedy(head: str, items: list, sep: str, tail: Callable[[int], str] = lambda left: "") -> str:
    """``head`` plus as many whole items as fit, and ``tail(n)`` for the ``n`` that did not."""
    text = head
    for index, item in enumerate(items):
        candidate = text + (sep if index else "") + item
        left = len(items) - index - 1
        if fits(candidate + tail(left)):
            text = candidate
            continue
        return text + tail(len(items) - index) if index else cap(candidate)
    return text


def _schedule(db, group, now: datetime) -> str:
    upcoming = group_bot._lessons(db, group, now).filter(
        Event.start_datetime < now + group_bot.SCHEDULE_HORIZON).all()
    if group.is_over and not upcoming:
        return "🗓 Курс группы завершён"
    slots = render.weekly_pattern(group.schedule_config, upcoming, now)
    if not slots:
        return "🗓 Расписание группы пока не заполнено"
    parts = [line.removeprefix("• ") for line in render.pattern_lines(slots, "ru")]
    text = _greedy("🗓 Расписание:\n", parts, "\n", lambda left: "\n…" if left else "")
    config = group.schedule_config if isinstance(group.schedule_config, dict) else {}
    try:
        start_date = date.fromisoformat(str(config.get("start_date")))
    except ValueError:
        start_date = None
    suffix = "\n\nНа этой неделе есть изменения — /schedule"
    if render.week_changes(slots, upcoming, now, "ru", start_date) and fits(text + suffix):
        text += suffix
    return text


def _lessons(db, group, now: datetime) -> str:
    lessons = group_bot._lessons(db, group, now).limit(group_bot.LESSONS_SHOWN).all()
    if not lessons:
        return "📅 Курс группы завершён" if group.is_over else "📅 Ближайших уроков пока нет"
    today = render.local(now).date()
    items = []
    for lesson in lessons:
        start = render.local(lesson.start_datetime)
        if lesson.start_datetime <= now:
            label = "Идёт сейчас"
        elif start.date() == today:
            label = "Сегодня"
        elif start.date() == today + timedelta(days=1):
            label = "Завтра"
        else:
            label = f"{render.WEEKDAYS['ru'][start.weekday()]} {start:%d.%m}"
        items.append(f"{label} {start:%H:%M}")
    return _greedy("📅 Ближайшие уроки:\n", items, "\n")


def _homework(db, group, now: datetime) -> str:
    tasks = group_bot._homework(db, group, now)
    if not tasks:
        return "📝 Открытых заданий нет"
    late = [task for task in tasks if task.due_date is not None and task.due_date < now]
    ordered = [task for task in tasks if task not in late] + late
    today = render.local(now).date()
    items = []
    for task in ordered:
        if task.due_date is None:
            items.append(f"• {short(task.title)} — без срока")
            continue
        due = render.local(task.due_date)
        if task in late:
            items.append(f"• {short(task.title)} — срок прошёл {due:%d.%m}")
            continue
        when = f"до {due:%d.%m %H:%M}"
        if due.date() == today:
            when += " (сегодня)"
        elif due.date() == today + timedelta(days=1):
            when += " (завтра)"
        items.append(f"• {short(task.title)} — {when}")
    return _greedy("📝 Домашние задания:\n", items, "\n", lambda left: f"\n+ ещё {left} — /homework" if left else "")


def popup(db, *, support_group_id: int, action: str, now: Optional[datetime] = None) -> dict:
    """Raises ``ValueError`` (unknown action), :class:`group_bot.NotLinked`, :class:`group_bot.SwitchedOff`."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    group, _is_test = group_bot.resolve(db, support_group_id)
    if not group_bot_settings.enabled_for(db, group):
        raise group_bot.SwitchedOff(f"the group bot is off for group {group.id}")
    build = {"schedule": _schedule, "lessons": _lessons, "homework": _homework}[action]
    return {"text": cap(build(db, group, now)), "show_alert": True}
