"""Tell staff, while the lesson is still on, that it is running without a recording.

Auto-recording starts only when someone from the organisation joins from a place that can
record. On 2026-09-16 Laila's lessons ran unrecorded: she joins on her work account, but from
the Meet app on an iPhone/iPad, and the app never starts the recording (tested live: the iPad
app never started one, Safari on the same iPad did within 7 s, but Safari cannot share the screen).
Teachers on a personal Google account, or late, leave the room unrecorded the same way. Nothing
noticed until MissingRecordingLog, hours later — by then the lesson is lost.

Anyone from the organisation joining from a computer browser starts the recording within
seconds, so the remedy is a person, fast: 3 minutes into the lesson, with people in the room
for at least a minute and no recording in the live call, one message goes to the staff chats
(TELEGRAM_RECORDING_ALERT_CHATS) with the join link. When the recording then starts, the same
chats get a reply saying so. ``ENABLE_RECORDING_WATCHDOG=true`` switches it on.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from src.schemas.models import GoogleAccountLink, RecordingStartAlert, UserInDB
from src.services import google_workspace, meet_recordings, telegram_service
from src.services.meet_room_closer import _pages

logger = logging.getLogger(__name__)

ALERT_AFTER_START = timedelta(minutes=3)
# A recording starts 2–7 s after the right person joins; a minute in the room rules out the race.
MIN_IN_ROOM = timedelta(minutes=1)
_ALMATY = ZoneInfo("Asia/Almaty")

Send = Callable[[str, str, Optional[int]], Optional[int]]


def enabled() -> bool:
    return os.getenv("ENABLE_RECORDING_WATCHDOG", "false").strip().lower() in ("1", "true", "yes", "on")


def _utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def _almaty(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).astimezone(_ALMATY).strftime("%H:%M")


def _hint(teacher_in_room: Optional[bool]) -> str:
    if teacher_in_room is None:
        return "Аккаунты учителя в Meet ещё не подтверждены — не видно, в комнате ли он."
    if teacher_in_room:
        return ("Учитель в комнате, но запись не стартует — скорее всего, он зашёл через приложение "
                "Meet на iPhone/iPad или с личного аккаунта.")
    return "Учителя в комнате нет — он опаздывает или зашёл с другого аккаунта."


def alert_text(*, title: str, teacher: Optional[str], start: datetime, end: datetime, people: int,
               minutes: int, teacher_in_room: Optional[bool], meeting_url: str) -> str:
    who = f" · {html.escape(teacher)}" if teacher else ""
    return (
        "🔴 <b>Урок идёт без записи</b>\n"
        f"<b>{html.escape(title)}</b>\n"
        f"{_almaty(start)}–{_almaty(end)}{who}\n"
        f"В комнате {people} чел., записи нет уже {minutes} мин.\n"
        f"{_hint(teacher_in_room)}\n\n"
        "👉 Зайдите в урок с рабочего аккаунта в браузере на компьютере — запись начнётся сама:\n"
        f"{html.escape(meeting_url)}"
    )


def started_text(started: datetime, lesson_start: datetime) -> str:
    late = max(0, int((started - lesson_start).total_seconds() // 60))
    return f"✅ Запись началась в {_almaty(started)} ({late} мин. от начала урока)."


def check_recordings_started(db, now: Optional[datetime] = None, send: Optional[Send] = None) -> int:
    """Alert on every live lesson that should be recording and is not. Returns alerts sent."""
    chats = telegram_service.recording_alert_chats()
    if not enabled() or not chats:
        return 0
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    send = send or telegram_service.send_message_sync
    meet = google_workspace.meet_client()
    alerted = 0
    for conference in _pages(meet.conferenceRecords().list, "conferenceRecords", filter="end_time IS NULL"):
        space = conference.get("space")
        try:
            lesson = meet_recordings.match_lesson(db, meet_recordings.space_meet_code(space or ""))
            if lesson is None or not lesson.start_datetime or not lesson.end_datetime:
                continue  # not a lesson room
            if not (lesson.start_datetime + ALERT_AFTER_START <= now <= lesson.end_datetime):
                continue  # too early to judge, or the lesson is over (the room closer's business)
            alert = db.query(RecordingStartAlert).filter(RecordingStartAlert.event_id == lesson.id).first()
            if alert is not None and alert.recording_started_at is not None:
                continue  # told, and already resolved
            teacher_accounts = {google_user for (google_user,) in db.query(GoogleAccountLink.google_user)
                                .filter(GoogleAccountLink.user_id == lesson.teacher_id)}
            teacher = db.query(UserInDB.name).filter(UserInDB.id == lesson.teacher_id).scalar()
            lesson_id, title = lesson.id, lesson.title
            start, end, meeting_url = lesson.start_datetime, lesson.end_datetime, lesson.meeting_url
            db.commit()  # nothing held open on the database while Google answers

            recordings = meet.conferenceRecords().recordings().list(parent=conference["name"]).execute()
            began = [t for t in (_utc(r.get("startTime")) for r in recordings.get("recordings", [])) if t]
            if began:
                if alert is not None:
                    _resolve(db, alert, min(began), start, send)
                continue
            if alert is not None:
                continue  # already told; wait for the recording

            still_in = _pages(meet.conferenceRecords().participants().list, "participants",
                              parent=conference["name"], filter="latest_end_time IS NULL")
            joined = [t for t in (_utc(p.get("earliestStartTime")) for p in still_in) if t]
            if not joined or now - min(joined) < MIN_IN_ROOM:
                continue  # empty room, or people only just arrived
            teacher_in_room = (None if not teacher_accounts else
                               any((p.get("signedinUser") or {}).get("user") in teacher_accounts for p in still_in))

            alert = RecordingStartAlert(event_id=lesson_id, conference_record=conference["name"],
                                        teacher_in_room=teacher_in_room, people_in_room=len(still_in),
                                        alerted_at=now)
            db.add(alert)
            try:
                db.commit()  # claim first: two ticks must never both send
            except IntegrityError:
                db.rollback()
                continue
            text = alert_text(title=title, teacher=teacher, start=start, end=end, people=len(still_in),
                              minutes=int((now - max(start, min(joined))).total_seconds() // 60),
                              teacher_in_room=teacher_in_room, meeting_url=meeting_url)
            alert.messages = [{"chat": chat, "message_id": message_id}
                              for chat in chats if (message_id := send(chat, text, None))]
            db.commit()
            alerted += 1
            logger.warning("lesson %s: running %s with %d in the room and no recording — staff alerted (%d chats)",
                           lesson_id, space, len(still_in), len(alert.messages))
        except Exception as e:
            db.rollback()
            logger.warning("recording watchdog, room %s: %s", space, e)
    return alerted


def _resolve(db, alert: RecordingStartAlert, started: datetime, lesson_start: datetime, send: Send) -> None:
    alert.recording_started_at = started
    db.commit()
    text = started_text(started, lesson_start)
    for sent in alert.messages or []:
        send(sent["chat"], text, sent["message_id"])
    logger.info("lesson %s: recording started %s, after the alert", alert.event_id, started)
