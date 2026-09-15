"""The morning digest and the last-chance reminder, in the owner's Gen-Z voice (owner, 2026-09-15).

**When.** A day whose first lesson starts at 10:00 Almaty or later gets its digest at 10:00 that
day; a day that starts earlier gets it the evening before, at 20:00 («не проспи»), and nothing at
10:00. A digest goes out only when there is something in it — a lesson still ahead that day (the
evening one: tomorrow's lessons) or a homework deadline in the next 24 hours. A tick that runs late
still sends within the hour.

**What.** No group name (owner): a rotating greeting, the lessons with their Meet links, the
deadlines, the weekly mock while it is open, a rotating closer. Rotation is a stable hash of the
group and the day, so a retried send says the same thing.

**Last chance.** Three hours before each homework deadline: «⏰ До дедлайна 3 часа…». In quiet hours
(23:00–08:00) it waits for 08:00 and is sent only if the deadline is still ahead, saying how much
time is actually left then. A moved deadline is a new reminder.

Every post is claimed in :class:`TelegramDigestSend` before Support is called. Off unless
``ENABLE_TELEGRAM_DIGEST`` is set; ``group_bot.digest_enabled`` / ``digest_off_groups`` switch it
off globally or per group.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from html import escape

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramDigestSend
from src.schemas.models import Assignment, Event, EventGroup, GroupAssignment
from src.services import group_bot_outbox as outbox
from src.services import group_bot_render as render
from src.services import group_bot_settings
from src.services.operational_groups import event_has_operational_group_clause

FLAG = "ENABLE_TELEGRAM_DIGEST"
MORNING, EVENING = time(10, 0), time(20, 0)
SEND_WINDOW = timedelta(minutes=60)
HORIZON = timedelta(hours=24)
LAST_CHANCE = timedelta(hours=3)
_OPEN = ("pending", "failed")

GREETINGS = (
    "☀️ Доброе утро! Сегодня есть движ 👀",
    "Гуд морнинг ☕️ Сверяем планы на день",
    "Проснулись? Вот что сегодня по учёбе 📚",
    "Утречко 🌤 Коротко, что нас ждёт",
    "Всем привет! Минутка планирования, не скипаем 😌",
)
CLOSERS = (
    "Вопросы? Тегай меня 🤖",
    "Всё получится, го 💪",
    "Если что — я тут, просто отметь меня",
    "Удачного дня и без дедлайн-паники 🫶",
    "Не прокрастинируем 😉",
)


def enabled(db) -> bool:
    return outbox.flag(FLAG) and bool(group_bot_settings.current(db).get("digest_enabled", True))


def pick(options: tuple, *parts) -> str:
    digest = hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()
    return options[int(digest, 16) % len(options)]


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _in_window(now_local: datetime, start: time) -> bool:
    opens = datetime.combine(now_local.date(), start)
    return opens <= now_local < opens + SEND_WINDOW


def day_lessons(db, group, day: date) -> list:
    start = datetime.combine(day, time()) - render.ALMATY_OFFSET
    return (db.query(Event).join(EventGroup, EventGroup.event_id == Event.id)
            .filter(EventGroup.group_id == group.id, Event.is_active.is_(True), Event.event_type == "class",
                    Event.start_datetime >= start, Event.start_datetime < start + timedelta(days=1),
                    event_has_operational_group_clause())
            .order_by(Event.start_datetime).all())


def deadlines(db, group, after: datetime, until: datetime) -> list:
    rows = (db.query(Assignment)
            .outerjoin(GroupAssignment, and_(GroupAssignment.assignment_id == Assignment.id,
                                             GroupAssignment.group_id == group.id,
                                             GroupAssignment.is_active.is_(True)))
            .filter(or_(Assignment.group_id == group.id, GroupAssignment.id.isnot(None)),
                    Assignment.is_active.is_(True), Assignment.is_hidden.is_(False),
                    Assignment.due_date > after, Assignment.due_date <= until)
            .order_by(Assignment.due_date, Assignment.id).all())
    seen, out = set(), []
    for row in rows:
        if row.id not in seen:
            seen.add(row.id)
            out.append(row)
    return out


def open_weekly(db, group, now: datetime):
    return (db.query(Event).join(EventGroup, EventGroup.event_id == Event.id)
            .filter(EventGroup.group_id == group.id, Event.is_active.is_(True), Event.event_type == "weekly_test",
                    Event.start_datetime <= now, Event.end_datetime > now, event_has_operational_group_clause())
            .order_by(Event.end_datetime).first())


def _lesson_lines(lessons: list, *, nudge: bool) -> list:
    lines = []
    for lesson in lessons:
        start, end = render.local(lesson.start_datetime), render.local(lesson.end_datetime)
        lines.append(f"📚 Урок в {start:%H:%M}–{end:%H:%M}" + (" — не проспи 😴" if nudge else ""))
        if lesson.meeting_url:
            lines.append(f"🔗 {escape(lesson.meeting_url)}")
    return lines


def _deadline_lines(tasks: list, now: datetime) -> list:
    today = render.local(now).date()
    lines = ["📝 Дедлайны:"]
    for task in tasks:
        due = render.local(task.due_date)
        if due.date() == today:
            day = "сегодня"
        elif due.date() == today + timedelta(days=1):
            day = "завтра"
        else:
            day = f"{due.day} {render.MONTHS['ru'][due.month - 1]}"
        lines.append(f"• <b>{escape(task.title or '')}</b> — {day} до {due:%H:%M}")
    return lines


def compose(opening: str, lessons: list, tasks: list, weekly, closer: str, now: datetime, *, nudge: bool) -> str:
    blocks = [opening]
    if lessons:
        blocks.append("\n".join(_lesson_lines(lessons, nudge=nudge)))
    if tasks:
        blocks.append("\n".join(_deadline_lines(tasks, now)))
    if weekly is not None and weekly.end_datetime is not None:
        blocks.append(f"🧪 Weekly mock открыт до {render.deadline_label(weekly.end_datetime, now, 'ru')}")
    blocks.append(closer)
    return "\n\n".join(blocks)


def morning_text(group, day: date, lessons: list, tasks: list, weekly, now: datetime) -> str:
    return compose(pick(GREETINGS, group.id, day.isoformat(), "greeting"), lessons, tasks, weekly,
                   pick(CLOSERS, group.id, day.isoformat(), "closer"), now, nudge=True)


def evening_text(group, day: date, lessons: list, tasks: list, weekly, now: datetime) -> str:
    first = render.local(lessons[0].start_datetime)
    opening = f"🌙 Напоминалка на завтра: урок уже в {first:%H:%M}, не проспи 😴"
    return compose(opening, lessons, tasks, weekly, pick(CLOSERS, group.id, day.isoformat(), "closer"),
                   now, nudge=False)


def time_left(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes >= 165:
        return "3 часа"
    hours, mins = divmod(minutes, 60)
    mins = mins // 5 * 5
    parts = [f"{hours} {plural(hours, 'час', 'часа', 'часов')}"] if hours else []
    if mins or not hours:
        mins = max(mins, 5)
        parts.append(f"{mins} {plural(mins, 'минута', 'минуты', 'минут')}")
    return " ".join(parts)


def last_chance_text(task, now: datetime) -> str:
    due = render.local(task.due_date)
    day = " (завтра)" if due.date() != render.local(now).date() else ""
    return (f"⏰ До дедлайна {time_left(task.due_date - now)}: <b>{escape(task.title or '')}</b> — "
            f"до {due:%H:%M}{day}. Кто ещё не сдал — самое время 🏃")


def _send(db, budget, summary, group, link, kind: str, key: str, text: str, now: datetime) -> None:
    row = (db.query(TelegramDigestSend)
           .filter(TelegramDigestSend.lms_group_id == group.id, TelegramDigestSend.kind == kind,
                   TelegramDigestSend.key == key).first())
    if row is None:
        row = TelegramDigestSend(lms_group_id=group.id, support_group_id=link.support_group_id, kind=kind,
                                 key=key, status="pending", attempts=0, created_at=now)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return
    if row.status not in _OPEN or row.attempts >= outbox.MAX_ATTEMPTS or not budget.take():
        return
    claimed = (db.query(TelegramDigestSend)
               .filter(TelegramDigestSend.id == row.id, TelegramDigestSend.status.in_(_OPEN))
               .update({"attempts": TelegramDigestSend.attempts + 1}, synchronize_session=False))
    db.commit()
    if not claimed:
        return
    db.refresh(row)
    outcome = outbox.post(link.support_group_id, text, f"digest:{kind}:{group.id}:{key}", silent=False)
    row.status, row.error = outcome["status"], outcome["error"]
    if outcome["status"] == "sent":
        row.telegram_message_id, row.sent_at = outcome["telegram_message_id"], now
    db.commit()
    summary[outcome["status"]] += 1


def run(db, live: list, budget: outbox.Budget, now: datetime) -> dict:
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    local = render.local(now)
    today = local.date()
    for group, link in live:
        if not group_bot_settings.digest_enabled_for(db, group.id):
            continue
        if _in_window(local, MORNING):
            lessons = day_lessons(db, group, today)
            early = bool(lessons) and render.local(lessons[0].start_datetime).time() < MORNING
            ahead = [lesson for lesson in lessons if lesson.start_datetime > now]
            tasks = deadlines(db, group, now, now + HORIZON)
            if not early and (ahead or tasks):
                text = morning_text(group, today, ahead, tasks, open_weekly(db, group, now), now)
                _send(db, budget, summary, group, link, "morning", today.isoformat(), text, now)
        if _in_window(local, EVENING):
            tomorrow = today + timedelta(days=1)
            lessons = day_lessons(db, group, tomorrow)
            if lessons and render.local(lessons[0].start_datetime).time() < MORNING:
                tasks = deadlines(db, group, now, now + HORIZON)
                text = evening_text(group, tomorrow, lessons, tasks, open_weekly(db, group, now), now)
                _send(db, budget, summary, group, link, "evening", tomorrow.isoformat(), text, now)
        if outbox.in_quiet_hours(now):
            continue
        for task in deadlines(db, group, now, now + LAST_CHANCE):
            send_at = task.due_date - LAST_CHANCE
            if outbox.in_quiet_hours(send_at):
                send_at = outbox.quiet_hours_end(send_at)
            if now >= send_at:
                key = f"{task.id}:{task.due_date.isoformat()}"
                _send(db, budget, summary, group, link, "last_chance", key, last_chance_text(task, now), now)
    return summary
