"""Public Telegram announcements for upcoming webinars and office hours.

The LMS owns the schedule and Meet URL; Support owns the Telegram bot. This job posts one
carefully formatted Russian announcement to the configured public channel 15 minutes before
each webinar. It is deliberately separate from group lesson invitations: these events are
course-wide and have no LMS group/chat mapping.
"""
import html
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from src.announcements.models import TelegramEventAnnouncement
from src.schemas.models import Event
from src.services import group_bot_outbox

logger = logging.getLogger(__name__)

ALMATY = ZoneInfo("Asia/Almaty")
LEAD = timedelta(minutes=15)
WINDOW = timedelta(minutes=5)
GRACE_AFTER_START = timedelta(minutes=10)
MAX_ATTEMPTS = 3
SYSTEM_ACTOR = "lms-event-announcements@mastereducation.kz"
ACTOR_NAME = "LMS event announcements"

RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря")


def target_support_group_id() -> Optional[int]:
    raw = os.getenv("TELEGRAM_EVENT_ANNOUNCEMENT_GROUP_ID", "").strip()
    try:
        value = int(raw)
        return value if value > 0 else None
    except ValueError:
        return None


def enabled() -> bool:
    return (
        os.getenv("ENABLE_TELEGRAM_EVENT_ANNOUNCEMENTS", "").strip().lower()
        in ("1", "true", "yes", "on")
        and target_support_group_id() is not None
    )


def _almaty(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ALMATY)


def _teacher_name(event: Event) -> str:
    teacher = getattr(event, "teacher", None)
    return (getattr(teacher, "official_full_name", None)
            or getattr(teacher, "name", None)
            or "преподаватель Master Education")


def _label(title: str) -> str:
    lowered = title.lower()
    if "sat verbal" in lowered:
        return "Консультация по SAT — Verbal"
    if "sat math" in lowered:
        return "Консультация по SAT — Math"
    if "writing" in lowered:
        return "IELTS Writing — Office Hours"
    if "speaking" in lowered:
        return "IELTS Speaking Club"
    return title


def announcement_text(event: Event) -> str:
    """Public-safe HTML message: no student names, group data or internal LMS details."""
    start, end = _almaty(event.start_datetime), _almaty(event.end_datetime)
    teacher = html.escape(_teacher_name(event), quote=False)
    label = html.escape(_label(event.title), quote=False)
    date = f"{RU_WEEKDAYS[start.weekday()]}, {start.day} {RU_MONTHS[start.month - 1]}"
    extra = ""
    if "writing" in event.title.lower():
        extra = "\n\nПриходите с готовым эссе и получите обратную связь и рекомендации в порядке живой очереди."
    elif "speaking" in event.title.lower():
        extra = "\n\nПриходите попрактиковать разговорный английский и получить обратную связь."
    else:
        extra = "\n\nПриходите со своими вопросами и получите помощь преподавателя в прямом эфире."
    url = html.escape(event.meeting_url or "", quote=True)
    return (
        "📣 <b>Напоминание о предстоящей встрече</b>\n\n"
        f"<b>{label}</b>\n"
        f"🗓 {date}\n"
        f"⏰ {start:%H:%M}–{end:%H:%M} (время Алматы)\n"
        f"👩‍🏫 Преподаватель: {teacher}"
        f"{extra}\n\n"
        f"🔗 <a href=\"{url}\">Подключиться к Google Meet</a>\n\n"
        "Пожалуйста, подключитесь за 2–3 минуты до начала."
    )


def due(db, now: datetime) -> list[Event]:
    target = target_support_group_id()
    if target is None:
        return []
    target_start = now + LEAD
    target_end = target_start + WINDOW
    rows = (
        db.query(Event)
        .outerjoin(TelegramEventAnnouncement, and_(
            TelegramEventAnnouncement.event_id == Event.id,
            TelegramEventAnnouncement.support_group_id == target,
        ))
        .filter(
            Event.event_type == "webinar",
            Event.is_active.is_(True),
            Event.meeting_url.ilike("https://meet.google.com/%"),
            Event.start_datetime >= target_start,
            Event.start_datetime <= target_end,
            or_(TelegramEventAnnouncement.id.is_(None), and_(
                TelegramEventAnnouncement.status.in_(("pending", "failed")),
                TelegramEventAnnouncement.attempts < MAX_ATTEMPTS,
            )),
        )
        .order_by(Event.start_datetime, Event.id)
        .all()
    )
    return rows


def _claim(db, event_id: int, target: int) -> Optional[int]:
    row = (db.query(TelegramEventAnnouncement)
           .filter_by(event_id=event_id, support_group_id=target).first())
    if row is None:
        row = TelegramEventAnnouncement(event_id=event_id, support_group_id=target, status="pending")
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
    elif row.status == "sent" or row.attempts >= MAX_ATTEMPTS:
        return None
    row.attempts += 1
    db.commit()
    return row.id


def send_due_announcements(db, now: Optional[datetime] = None) -> dict:
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    if not enabled():
        return summary
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    target = target_support_group_id()
    for event in due(db, now):
        announcement_id = _claim(db, event.id, target)
        if announcement_id is None:
            continue
        outcome = group_bot_outbox.post(
            target,
            announcement_text(event),
            f"event-announcement:{event.id}:{target}",
            silent=False,
        )
        row = db.get(TelegramEventAnnouncement, announcement_id)
        row.status = outcome["status"]
        row.telegram_message_id = outcome.get("telegram_message_id")
        row.error = outcome.get("error")
        if outcome["status"] == "sent":
            row.sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        summary[outcome["status"]] += 1
        logger.info("event %s → public chat %s: %s", event.id, target, outcome["status"])
    return summary
