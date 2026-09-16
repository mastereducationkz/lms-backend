"""Staff "Meet notifications": what is going wrong in a live lesson, said while it can be fixed.

Owner, 2026-09-16. The Support bot posts them into a topic of the curators' group
(``MEET_NOTICES_SUPPORT_GROUP_ID`` + ``MEET_NOTICES_TOPIC_ID``: «Кураторы Master Education» →
«Meet Notifications»); the LMS report bot never posts there. Every minute, beside the room
closer, for each lesson running in one of our Meet rooms:

- 🟠 ``teacher_absent`` — 5 min in, people in the room and none of the teacher's confirmed Google
  accounts among them: late, or joined on another account. It says outright whether the lesson is
  recording, and while it is open it stands in for the no-recording notice — a missing teacher is
  why nothing records. Replied to once the teacher has been in the room a minute.
- 🔴 ``no_recording`` — 3 min in, people in the room for a minute, no recording in the call.
  Auto-recording starts only for an organisation account on a computer browser: the Meet app on an
  iPhone/iPad never starts it, not even on a work account (tested live 2026-09-16). Replied to when
  the recording starts.
- ⚪ ``empty_room`` — 10 min in, nobody has come to the room at all: cancelled, or another link.

The evening summary lives in :mod:`src.services.meet_staff_digest`. A notice is one
:class:`MeetStaffNotice` per lesson and kind, claimed before Support is called and retried under
the same idempotency key. ``ENABLE_MEET_STAFF_NOTICES=true`` switches it all on.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Optional

from sqlalchemy.exc import IntegrityError

from src.schemas.models import Event, GoogleAccountLink, MeetStaffNotice, UserInDB
from src.services import google_workspace, meet_recordings
from src.services import group_bot_outbox as outbox
from src.services import group_bot_render as render
from src.services.meet_room_closer import _pages
from src.services.operational_groups import event_has_operational_group_clause
from src.services.telegram_invitations import _held_in_an_lms_meet_room

logger = logging.getLogger(__name__)

NO_RECORDING_AFTER = timedelta(minutes=3)
TEACHER_ABSENT_AFTER = timedelta(minutes=5)
EMPTY_ROOM_AFTER = timedelta(minutes=10)
# A recording starts 2–7 s after the right person joins: a minute in the room rules out the race.
SETTLE = timedelta(minutes=1)
# A teacher who arrives in the last minutes still gets the reply.
RESOLVE_GRACE = timedelta(minutes=10)
# Calls this far before the lesson are still its visitors (meet_presence.LESSON_MARGIN).
EARLY_VISIT = timedelta(minutes=30)
MAX_ATTEMPTS = 3
LESSON_KINDS = ("no_recording", "teacher_absent", "empty_room")

# Lessons whose room somebody has already visited: never "empty", so not asked about again.
_VISITED: set = set()


def target() -> Optional[tuple]:
    try:
        group_id = int(os.getenv("MEET_NOTICES_SUPPORT_GROUP_ID", ""))
        topic_id = int(os.getenv("MEET_NOTICES_TOPIC_ID", "0") or 0) or None
    except ValueError:
        return None
    return group_id, topic_id


def enabled() -> bool:
    return outbox.flag("ENABLE_MEET_STAFF_NOTICES") and target() is not None


# --- what the messages say -------------------------------------------------------------------


def _hhmm(value: datetime) -> str:
    return render.local(value).strftime("%H:%M")


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def lesson_lines(lesson: dict) -> str:
    who = f" · {escape(lesson['teacher'])}" if lesson.get("teacher") else ""
    return f"<b>{escape(lesson['title'])}</b>\n{_hhmm(lesson['start'])}–{_hhmm(lesson['end'])}{who}"


def teacher_absent_text(lesson: dict, *, now: datetime, people: int, recording: bool) -> str:
    return (
        "🟠 <b>Учитель не зашёл в урок</b>\n"
        f"{lesson_lines(lesson)}\n"
        f"Прошло {_minutes(now - lesson['start'])} мин. В комнате {people} чел., рабочего аккаунта "
        "учителя среди них нет — опаздывает или зашёл с другого (личного) аккаунта.\n"
        + ("✅ Запись идёт.\n" if recording else "❌ <b>Урок не записывается.</b>\n")
        + f"\n👉 {escape(lesson['url'])}"
    )


def no_recording_text(lesson: dict, *, now: datetime, people: int, since: datetime,
                      teacher_in_room: Optional[bool]) -> str:
    if teacher_in_room:
        hint = ("Учитель в комнате, но запись не стартует — скорее всего, он зашёл через приложение "
                "Meet на телефоне или планшете, или с личного аккаунта.")
    else:
        hint = "Аккаунты учителя в Meet ещё не подтверждены — не видно, в комнате ли он."
    return (
        "🔴 <b>Урок идёт без записи</b>\n"
        f"{lesson_lines(lesson)}\n"
        f"В комнате {people} чел., записи нет уже {_minutes(now - max(lesson['start'], since))} мин.\n"
        f"{hint}\n\n"
        "👉 Запись запускается, когда в урок заходит рабочий аккаунт из браузера на компьютере:\n"
        f"{escape(lesson['url'])}"
    )


def empty_room_text(lesson: dict, *, now: datetime) -> str:
    return (
        "⚪ <b>В уроке никого нет</b>\n"
        f"{lesson_lines(lesson)}\n"
        f"Прошло {_minutes(now - lesson['start'])} мин. с начала — в комнату никто не заходил. "
        "Урок отменён или у группы другая ссылка?\n"
        f"{escape(lesson['url'])}"
    )


def teacher_joined_text(joined: datetime, *, recording: bool) -> str:
    return f"✅ Учитель зашёл в {_hhmm(joined)}. " + ("Запись идёт." if recording else "❌ Записи всё ещё нет.")


def recording_started_text(started: datetime, lesson_start: datetime) -> str:
    return f"✅ Запись началась в {_hhmm(started)} ({_minutes(started - lesson_start)} мин. от начала урока)."


# --- delivery --------------------------------------------------------------------------------


def _deliver(db, notice_id: int, text: str, key: str) -> str:
    """Send (or re-send) a notice's message → its status."""
    group_id, topic_id = target()
    row = db.get(MeetStaffNotice, notice_id)
    row.attempts += 1
    db.commit()  # nothing held open across the network call
    result = outbox.post(group_id, text, key, silent=False, topic_id=topic_id)
    row = db.get(MeetStaffNotice, notice_id)
    row.status, row.error = result["status"], result.get("error")
    row.telegram_message_id = result.get("telegram_message_id") or row.telegram_message_id
    db.commit()
    (logger.info if row.status == "sent" else logger.warning)(
        "meet notice %s (%s, lesson %s): %s %s", notice_id, row.kind, row.event_id, row.status, row.error or "")
    return row.status


def _deliver_reply(db, notice_id: int) -> None:
    group_id, topic_id = target()
    row = db.get(MeetStaffNotice, notice_id)
    row.reply_attempts += 1
    text, reply_to = (row.details or {}).get("reply_text"), row.telegram_message_id
    db.commit()
    result = outbox.post(group_id, text, f"meet-notice:{notice_id}:resolved", silent=True,
                         topic_id=topic_id, reply_to=reply_to)
    row = db.get(MeetStaffNotice, notice_id)
    row.reply_status = result["status"]
    db.commit()


def _retryable(status: Optional[str], attempts: int) -> bool:
    return status in (None, "pending", "failed") and attempts < MAX_ATTEMPTS


def _claim(db, kind: str, event_id: int, now: datetime, details: dict) -> Optional[int]:
    notice = MeetStaffNotice(kind=kind, event_id=event_id, created_at=now, details=details)
    db.add(notice)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # another tick claimed it first
        return None
    return notice.id


def _raise(db, kind: str, lesson: dict, now: datetime, text: str, summary: Counter) -> None:
    notice_id = _claim(db, kind, lesson["id"], now, {"text": text})
    if notice_id is not None:
        summary[f"{kind}:{_deliver(db, notice_id, text, f'meet-notice:{notice_id}')}"] += 1


def _keep_going(db, notice: MeetStaffNotice, summary: Counter) -> None:
    """Finish what an earlier tick started: an unsent message, an unsent resolution reply."""
    if notice.resolved_at is None and _retryable(notice.status, notice.attempts):
        text = (notice.details or {}).get("text")
        if text:
            summary[f"{notice.kind}:{_deliver(db, notice.id, text, f'meet-notice:{notice.id}')}"] += 1
    elif (notice.resolved_at is not None and notice.status == "sent" and notice.telegram_message_id
          and _retryable(notice.reply_status, notice.reply_attempts)):
        _deliver_reply(db, notice.id)


def _resolve(db, notice: MeetStaffNotice, now: datetime, reply_text: str) -> None:
    notice_id = notice.id
    row = db.get(MeetStaffNotice, notice_id)
    row.resolved_at = now
    row.details = {**(row.details or {}), "reply_text": reply_text}
    db.commit()
    if row.status == "sent" and row.telegram_message_id:
        _deliver_reply(db, notice_id)


# --- the minute tick -------------------------------------------------------------------------


def running_lessons(db, now: datetime) -> list:
    rows = (
        db.query(Event, UserInDB.name)
        .outerjoin(UserInDB, UserInDB.id == Event.teacher_id)
        .filter(
            Event.event_type == "class",
            Event.is_active.is_(True),
            Event.start_datetime <= now,
            Event.end_datetime + RESOLVE_GRACE >= now,
            _held_in_an_lms_meet_room(),
            event_has_operational_group_clause(),
        )
        .order_by(Event.start_datetime, Event.id)
        .all()
    )
    return [{"id": e.id, "title": e.title, "start": e.start_datetime, "end": e.end_datetime,
             "url": e.meeting_url, "code": meet_recordings.meet_code(e.meeting_url),
             "teacher_id": e.teacher_id, "teacher": name} for e, name in rows]


def _utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def _anyone_came(meet, lesson: dict) -> bool:
    if lesson["id"] in _VISITED:
        return True
    calls = _pages(meet.conferenceRecords().list, "conferenceRecords",
                   filter=f'space.meeting_code="{lesson["code"]}"')
    came = any((_utc(c.get("startTime")) or datetime.min) >= lesson["start"] - EARLY_VISIT for c in calls)
    if came:
        _VISITED.add(lesson["id"])
    return came


def check_lesson(db, meet, lesson: dict, conference: Optional[dict], notices: dict, teacher_accounts: set,
                 now: datetime, summary: Counter) -> None:
    for notice in notices.values():
        _keep_going(db, notice, summary)
    db.commit()  # nothing held open while Google answers
    elapsed, running = now - lesson["start"], now <= lesson["end"]

    still_in, recording_since = [], None
    if conference is not None:
        still_in = _pages(meet.conferenceRecords().participants().list, "participants",
                          parent=conference["name"], filter="latest_end_time IS NULL")
        recordings = meet.conferenceRecords().recordings().list(parent=conference["name"]).execute()
        starts = [t for t in (_utc(r.get("startTime")) for r in recordings.get("recordings", [])) if t]
        recording_since = min(starts) if starts else None
    arrivals = [(_utc(p.get("earliestStartTime")), (p.get("signedinUser") or {}).get("user")) for p in still_in]
    arrived = min((t for t, _ in arrivals if t), default=None)
    teacher_since = min((t for t, account in arrivals if t and account in teacher_accounts), default=None)
    teacher_in_room = None if not teacher_accounts else teacher_since is not None
    if arrived is not None:
        _VISITED.add(lesson["id"])

    absent, silent = notices.get("teacher_absent"), notices.get("no_recording")
    if absent is not None and absent.resolved_at is None and teacher_since and now - teacher_since >= SETTLE:
        _resolve(db, absent, now, teacher_joined_text(teacher_since, recording=recording_since is not None))
    if silent is not None and silent.resolved_at is None and recording_since:
        _resolve(db, silent, now, recording_started_text(recording_since, lesson["start"]))
    if not running:
        return

    if arrived is not None and now - arrived >= SETTLE:
        if absent is None and teacher_in_room is False and elapsed >= TEACHER_ABSENT_AFTER:
            _raise(db, "teacher_absent", lesson, now,
                   teacher_absent_text(lesson, now=now, people=len(still_in), recording=recording_since is not None),
                   summary)
            return  # it says whether the lesson records; the red notice waits for the teacher
        # Red is for a teacher who is there (or cannot be seen): an absent one gets orange at 5 min.
        absent_open = absent is not None and absent.resolved_at is None
        if (silent is None and recording_since is None and elapsed >= NO_RECORDING_AFTER
                and teacher_in_room is not False and not absent_open):
            _raise(db, "no_recording", lesson, now,
                   no_recording_text(lesson, now=now, people=len(still_in), since=arrived,
                                     teacher_in_room=teacher_in_room),
                   summary)
    elif (not still_in and "empty_room" not in notices and elapsed >= EMPTY_ROOM_AFTER
          and not any(k in notices for k in LESSON_KINDS) and not _anyone_came(meet, lesson)):
        _raise(db, "empty_room", lesson, now, empty_room_text(lesson, now=now), summary)


def run(db, now: Optional[datetime] = None) -> dict:
    """One tick: every running lesson checked, every due notice sent. Returns counts by kind:status."""
    if not enabled():
        return {}
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    summary: Counter = Counter()
    lessons = running_lessons(db, now)
    ids = [lesson["id"] for lesson in lessons]
    notices: dict = {}
    for notice in db.query(MeetStaffNotice).filter(MeetStaffNotice.event_id.in_(ids)).all() if ids else []:
        notices.setdefault(notice.event_id, {})[notice.kind] = notice
    teacher_ids = {lesson["teacher_id"] for lesson in lessons if lesson["teacher_id"]}
    accounts: dict = {}
    for google_user, user_id in (db.query(GoogleAccountLink.google_user, GoogleAccountLink.user_id)
                                 .filter(GoogleAccountLink.user_id.in_(teacher_ids)).all() if teacher_ids else []):
        accounts.setdefault(user_id, set()).add(google_user)
    db.commit()

    if lessons:
        meet = google_workspace.meet_client()
        live = {}
        for conference in _pages(meet.conferenceRecords().list, "conferenceRecords", filter="end_time IS NULL"):
            code = meet_recordings.space_meet_code(conference.get("space") or "")
            if code:
                live[code] = conference
        for lesson in lessons:
            if not lesson["code"]:
                continue
            try:
                check_lesson(db, meet, lesson, live.get(lesson["code"]), notices.get(lesson["id"], {}),
                             accounts.get(lesson["teacher_id"], set()), now, summary)
            except Exception as e:
                db.rollback()
                logger.warning("meet notices, lesson %s: %s", lesson["id"], e)

    from src.services import meet_staff_digest
    try:
        status = meet_staff_digest.send_if_due(db, now)
        if status:
            summary[f"digest:{status}"] += 1
    except Exception as e:
        db.rollback()
        logger.warning("meet digest: %s", e)
    return dict(summary)
