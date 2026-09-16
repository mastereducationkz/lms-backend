"""The day's Meet summary for staff, at 22:00 Almaty (owner, 2026-09-16).

Same topic and bot as the live notices (:mod:`src.services.meet_staff_notices`). For the day's
lessons in our Meet rooms: which ran without a recording, which nobody came to, whose teacher was
late / ended early / never joined, and how the day's live notices went.

Recording is read from ``lesson_recordings`` when the pipeline already claimed the lesson, and from
Meet otherwise (Google shows a recording the moment it starts, the claim comes an hour later).
Punctuality is :mod:`src.services.meet_presence` — lessons that ended in the last ~20 minutes are
not judged there yet, and the summary says how many.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from html import escape
from typing import Optional

from sqlalchemy import and_

from src.schemas.models import Event, LessonRecording, MeetStaffNotice, UserInDB
from src.services import google_workspace, meet_presence, meet_recordings
from src.services import group_bot_render as render
from src.services import meet_staff_notices as notices
from src.services.operational_groups import event_has_operational_group_clause
from src.services.telegram_invitations import _held_in_an_lms_meet_room

DIGEST_AT = time(22, 0)
LINES_PER_SECTION = 12
TEXT_LIMIT = 3900


def day_lessons(db, day: date, now: datetime) -> list:
    start = datetime.combine(day, time(0)) - render.ALMATY_OFFSET
    return (
        db.query(Event)
        .filter(
            Event.event_type == "class",
            Event.is_active.is_(True),
            Event.start_datetime >= start,
            Event.start_datetime < start + timedelta(days=1),
            Event.start_datetime <= now,
            _held_in_an_lms_meet_room(),
            event_has_operational_group_clause(),
        )
        .order_by(Event.start_datetime, Event.id)
        .all()
    )


def call_state(meet, lesson: dict) -> str:
    """``recorded`` | ``no_recording`` | ``no_call`` — what Meet saw for the lesson's room."""
    calls = notices.lesson_calls(meet, lesson)
    if not calls:
        return "no_call"
    for call in calls:
        if meet.conferenceRecords().recordings().list(parent=call["name"]).execute().get("recordings"):
            return "recorded"
    return "no_recording"


def _line(lesson: dict, teacher: Optional[str], tail: str = "") -> str:
    who = f" — {escape(teacher)}" if teacher else ""
    return f"• {render.local(lesson['start']):%H:%M} {escape(lesson['title'])}{who}{tail}"


def _section(title: str, lines: list) -> list:
    if not lines:
        return []
    shown = lines[:LINES_PER_SECTION]
    more = [f"…и ещё {len(lines) - len(shown)}"] if len(lines) > len(shown) else []
    return ["", title, *shown, *more]


def digest_text(db, day: date, now: datetime, meet=None) -> Optional[str]:
    events = day_lessons(db, day, now)
    if not events:
        return None
    lessons = [{"id": e.id, "title": e.title, "start": e.start_datetime, "end": e.end_datetime,
                "code": meet_recordings.meet_code(e.meeting_url), "teacher_id": e.teacher_id} for e in events]
    by_id = {lesson["id"]: lesson for lesson in lessons}
    ids = list(by_id)
    teachers = dict(db.query(UserInDB.id, UserInDB.name)
                    .filter(UserInDB.id.in_({l["teacher_id"] for l in lessons if l["teacher_id"]})).all())
    claimed = {event_id for (event_id,) in db.query(LessonRecording.event_id)
               .filter(LessonRecording.event_id.in_(ids), LessonRecording.status != "missing")}

    late, early, absent, pending = [], [], [], 0
    for record in meet_presence.records(db, events, now):
        if record["state"] == "waiting":
            pending += 1
        teacher = record.get("teacher") or {}
        lesson = by_id[record["event_id"]]
        for flag in teacher.get("flags", []):
            if flag["code"] == "teacher_late":
                late.append(_line(lesson, teacher.get("name"), f", на {flag['minutes']} мин."))
            elif flag["code"] == "ended_early":
                early.append(_line(lesson, teacher.get("name"), f", на {flag['minutes']} мин."))
            elif flag["code"] == "teacher_not_joined":
                absent.append(_line(lesson, teacher.get("name")))

    counts, solved = Counter(), Counter()
    for kind, resolved_at in (db.query(MeetStaffNotice.kind, MeetStaffNotice.resolved_at)
                              .filter(MeetStaffNotice.event_id.in_(ids)).all()):
        counts[kind] += 1
        solved[kind] += resolved_at is not None
    db.commit()  # the Meet calls below can take minutes; no transaction may sit open through them

    meet = meet or google_workspace.meet_client()
    unrecorded, empty = [], []
    for lesson in lessons:
        if lesson["id"] in claimed or not lesson["code"]:
            continue
        state = call_state(meet, lesson)
        if state == "no_recording":
            unrecorded.append(_line(lesson, teachers.get(lesson["teacher_id"])))
        elif state == "no_call":
            empty.append(_line(lesson, teachers.get(lesson["teacher_id"])))

    recorded = len(lessons) - len(unrecorded) - len(empty)
    parts = [f"📋 <b>Meet — итоги дня, {day:%d.%m}</b>",
             f"Уроков в Meet: {len(lessons)} · с записью {recorded} · без записи {len(unrecorded)}"
             + (f" · пустых {len(empty)}" if empty else "")]
    parts += _section("❌ <b>Без записи</b>", unrecorded)
    parts += _section("⚪ <b>Никто не заходил</b>", empty)
    parts += _section("🟠 <b>Учитель не зашёл</b>", absent)
    parts += _section("⏰ <b>Учитель опоздал</b>", late)
    parts += _section("🚪 <b>Закончили раньше</b>", early)
    if not (unrecorded or empty or absent or late or early):
        parts += ["", "Всё в порядке: все уроки записаны, опозданий нет."]
    if pending:
        parts += ["", f"⏳ Опоздания ещё не посчитаны для {pending} недавно закончившихся уроков."]
    if counts:
        signal = " · ".join(f"{icon} {counts[kind]} (решено {solved[kind]})"
                            for kind, icon in (("no_recording", "🔴"), ("teacher_absent", "🟠"), ("empty_room", "⚪"))
                            if counts[kind])
        parts += ["", f"Сигналы за день: {signal}"]
    text = "\n".join(parts)
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT].rsplit("\n", 1)[0] + "\n…"


def send_if_due(db, now: datetime, meet=None) -> Optional[str]:
    """Send today's summary once it is 22:00 Almaty → its status, or None when nothing was due."""
    local = render.local(now)
    if local.time() < DIGEST_AT:
        return None
    day = local.date()
    notice = db.query(MeetStaffNotice).filter(and_(MeetStaffNotice.kind == "digest", MeetStaffNotice.day == day)).first()
    if notice is not None and not notices._retryable(notice.status, notice.attempts):
        return None
    text = digest_text(db, day, now, meet)
    if notice is None:
        notice = MeetStaffNotice(kind="digest", day=day, created_at=now,
                                 status="pending" if text else "skipped")
        db.add(notice)
        try:
            db.commit()
        except Exception:
            db.rollback()
            return None
    if not text:
        row = db.get(MeetStaffNotice, notice.id)
        row.status = "skipped"
        db.commit()
        return "skipped"
    return notices._deliver(db, notice.id, text, f"meet-digest:{day.isoformat()}")
