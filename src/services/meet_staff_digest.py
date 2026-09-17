"""The day's Meet summary for staff, at 22:00 Almaty (owner, 2026-09-16).

Same topic and bot as the live notices (:mod:`src.services.meet_staff_notices`). For the day's
lessons in our Meet rooms: which ran without a recording, which nobody came to, whose teacher was
late / ended early / never joined, and how the day's live notices went.

Recording is read from ``lesson_recordings`` when the pipeline already claimed the lesson, and from
Meet otherwise (Google shows a recording the moment it starts, the claim comes an hour later).
Punctuality is :mod:`src.services.meet_presence` — lessons that ended in the last ~20 minutes are
not judged there yet, and the summary says how many.

**A summary stays true for a day** (owner, 2026-09-16: a lesson moved to 20:10 after 22:00 left
«опоздал на 14 мин.» standing). Until the next summary goes out it is re-read every
``REFRESH_EVERY``, and within a minute when one of that day's lessons is edited; when it reads
differently the message is edited in place and — since Telegram tells nobody about an edit — a
silent reply lists what changed (➖ / ➕ per section).

**A lesson that took place stays in its day** (owner, 2026-09-17): on 16.09 a group was marked finished
after its last lesson, and that recorded lesson silently left the summary (60 → 59). The day's lessons
are those of an operational group *or* those that really ran — a saved Meet call over the lesson, or a
recording. And the reply names every lesson that joined or left the day, with why.
"""
from __future__ import annotations

from collections import Counter
import logging
from datetime import date, datetime, time, timedelta
from html import escape
from typing import Optional

from sqlalchemy import and_, exists, func, or_

from src.schemas.models import (Event, EventGroup, Group, GroupStudent, LessonRecording, MeetConference,
                                MeetStaffNotice, UserInDB)
from src.services import google_workspace, meet_presence, meet_recordings
from src.services import group_bot_outbox as outbox
from src.services import group_bot_render as render
from src.services import meet_staff_notices as notices
from src.services.operational_groups import event_has_operational_group_clause
from src.services.telegram_invitations import _held_in_an_lms_meet_room

logger = logging.getLogger(__name__)

DIGEST_AT = time(22, 0)
REFRESH_EVERY = timedelta(minutes=15)
# Until the next day's summary takes over.
LIVE_FOR = timedelta(hours=24)
LINES_PER_SECTION = 12
TEXT_LIMIT = 3900


def _took_place_clause():
    """The lesson really ran: a saved Meet call overlapping it, or a recording of it."""
    return or_(
        exists().where(and_(MeetConference.event_id == Event.id,
                            MeetConference.started_at < Event.end_datetime,
                            or_(MeetConference.ended_at.is_(None), MeetConference.ended_at > Event.start_datetime))
                       ).correlate(Event),
        exists().where(and_(LessonRecording.event_id == Event.id, LessonRecording.status != "missing")).correlate(Event),
    )


def _day_window(day: date) -> tuple:
    start = datetime.combine(day, time(0)) - render.ALMATY_OFFSET
    return start, start + timedelta(days=1)


def day_lessons(db, day: date, now: datetime) -> list:
    start, end = _day_window(day)
    return (
        db.query(Event)
        .filter(
            Event.event_type == "class",
            Event.is_active.is_(True),
            Event.start_datetime >= start,
            Event.start_datetime < end,
            Event.start_datetime <= now,
            _held_in_an_lms_meet_room(),
            # A group finished or turned off later does not take back a lesson it held (owner, 2026-09-17).
            or_(event_has_operational_group_clause(), _took_place_clause()),
        )
        .order_by(Event.start_datetime, Event.id)
        .all()
    )


def lesson_lines(db, day: date, now: datetime) -> dict:
    """{event id (str): its bullet line} for the day's lessons — what a summary remembers to name changes."""
    events = day_lessons(db, day, now)
    teachers = dict(db.query(UserInDB.id, UserInDB.name)
                    .filter(UserInDB.id.in_({e.teacher_id for e in events if e.teacher_id} or {-1})).all())
    return {str(e.id): _line({"title": e.title, "start": e.start_datetime}, teachers.get(e.teacher_id))
            for e in events}


def _kept_only_because_it_ran(db, ids: list) -> set:
    """Of these lessons, the ones in the day only because they ran — their groups are no longer operational."""
    if not ids:
        return set()
    operational = {eid for (eid,) in db.query(Event.id).filter(Event.id.in_(ids), event_has_operational_group_clause())}
    return {eid for eid in ids if eid not in operational}


def _groups_note(db, event_id: int) -> str:
    groups = db.query(Group).join(EventGroup, EventGroup.group_id == Group.id).filter(EventGroup.event_id == event_id).all()
    if not groups:
        return "урок без группы"
    if all(g.is_over for g in groups):
        return "группа завершена"
    if all(g.is_active is False for g in groups):
        return "группа отключена"
    has_students = (db.query(GroupStudent.id).join(UserInDB, UserInDB.id == GroupStudent.student_id)
                    .filter(GroupStudent.group_id.in_([g.id for g in groups]), UserInDB.is_active.is_(True)).first())
    return "в группе не осталось активных учеников" if has_students is None else "группа больше не в работе"


def why_left(db, event_id: int, day: date, now: datetime) -> str:
    """Why a lesson that was in the day's summary is not any more."""
    event = db.get(Event, event_id)
    if event is None:
        return "урок удалён"
    start, end = _day_window(day)
    if not event.is_active:
        return "урок отменён"
    if not (start <= event.start_datetime < end):
        return f"перенесён на {render.local(event.start_datetime):%d.%m в %H:%M}"
    if event.start_datetime > now:
        return f"перенесён на {render.local(event.start_datetime):%H:%M}, ещё не начался"
    if not db.query(Event.id).filter(Event.id == event_id, _held_in_an_lms_meet_room()).first():
        return "урок больше не в комнате LMS Meet"
    if event.event_type != "class":
        return "больше не урок группы"
    return _groups_note(db, event_id)


def why_joined(db, event_id: int, since: Optional[datetime], kept: set) -> str:
    """Why a lesson is in the day's summary now and was not before."""
    event = db.get(Event, event_id)
    if event_id in kept:
        return f"урок прошёл — остаётся в сводке, хотя {_groups_note(db, event_id)}"
    if since is not None and event.created_at is not None and event.created_at.replace(tzinfo=None) > since:
        return "новый урок"
    if since is not None and event.updated_at is not None and event.updated_at.replace(tzinfo=None) > since:
        return "урок изменён: перенесён на этот день или восстановлен"
    return "снова учитывается в сводке"


def call_state(meet, lesson: dict) -> str:
    """``recorded`` | ``no_recording`` | ``no_call`` — what Meet saw for the lesson's room."""
    calls = notices.lesson_calls(meet, lesson)
    if not calls:
        return "no_call"
    for call in calls:
        if meet.conferenceRecords().recordings().list(parent=call["name"]).execute().get("recordings"):
            return "recorded"
    # A call nobody joined — the link opened by a chat's preview — is still an empty room (2026-09-17).
    if not notices.lesson_visitors(meet, lesson):
        return "no_call"
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
    lessons = lesson_lines(db, day, now)
    status = notices._deliver(db, notice.id, text, f"meet-digest:{day.isoformat()}")
    if status == "sent":
        _remember(db, notice.id, text=text, rendered_at=now, lessons=lessons)
    return status


# --- keeping a sent summary true ---------------------------------------------------------------


def _remember(db, notice_id: int, **changes) -> None:
    row = db.get(MeetStaffNotice, notice_id)
    details = dict(row.details or {})
    for key, value in changes.items():
        if value is None:
            details.pop(key, None)
        else:
            details[key] = value.isoformat() if isinstance(value, datetime) else value
    row.details = details
    db.commit()


def _summary_start(day: date) -> datetime:
    return datetime.combine(day, DIGEST_AT) - render.ALMATY_OFFSET


def _lessons_edited_since(db, day: date, since: datetime) -> bool:
    start = datetime.combine(day, time(0)) - render.ALMATY_OFFSET
    latest = (db.query(func.max(Event.updated_at))
              .filter(Event.event_type == "class", Event.start_datetime >= start,
                      Event.start_datetime < start + timedelta(days=1))
              .scalar())
    return latest is not None and latest.replace(tzinfo=None) > since


def _read(text: str) -> tuple:
    """A rendered summary → (its counts line, {section title: [bullet lines]})."""
    head, *blocks = text.split("\n\n")
    head_lines = head.split("\n")
    sections = {}
    for block in blocks:
        lines = block.split("\n")
        if len(lines) > 1 and lines[0].endswith("</b>"):
            sections[lines[0]] = [line for line in lines[1:] if line.startswith("• ")]
    return (head_lines[1] if len(head_lines) > 1 else ""), sections


LESSONS_TITLE = "📚 <b>Уроки дня</b>"


def changes_text(day: date, old: str, new: str, left: tuple = (), joined: tuple = ()) -> Optional[str]:
    """What a reader of the old summary needs to know → the reply, or None when only notes moved.

    ``left`` / ``joined``: bullet lines of lessons that left or joined the day, each ending «: why»
    (owner, 2026-09-17: «Было 60 · Стало 59» alone did not say which lesson or why)."""
    old_counts, old_sections = _read(old)
    new_counts, new_sections = _read(new)
    parts = []
    if old_counts != new_counts:
        parts += [f"Было: {old_counts}", f"Стало: {new_counts}"]
    if left or joined:
        parts += ["", LESSONS_TITLE, *(f"➖ {line[2:]}" for line in left), *(f"➕ {line[2:]}" for line in joined)]
    titles = list(new_sections) + [t for t in old_sections if t not in new_sections]
    for title in titles:
        before, after = old_sections.get(title, []), new_sections.get(title, [])
        removed = [line for line in before if line not in after]
        added = [line for line in after if line not in before]
        if removed or added:
            parts += ["", title, *(f"➖ {line[2:]}" for line in removed), *(f"➕ {line[2:]}" for line in added)]
    if not parts:
        return None
    text = f"✏️ <b>Сводка за {day:%d.%m} обновлена</b>\n" + "\n".join(parts)
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT].rsplit("\n", 1)[0] + "\n…"


def _post_pending_reply(notice_id: int, db, day: date, message_id: int) -> None:
    row = db.get(MeetStaffNotice, notice_id)
    details = row.details or {}
    reply, number = details.get("pending_reply"), details.get("pending_update")
    if not reply:
        return
    group_id, topic_id = notices.target()
    db.commit()
    result = outbox.post(group_id, reply, f"meet-digest:{day.isoformat()}:update:{number}", silent=True,
                         topic_id=topic_id, reply_to=message_id)
    if result["status"] == "sent":
        _remember(db, notice_id, pending_reply=None, pending_update=None, updates=number)
    elif result["status"] == "skipped":
        _remember(db, notice_id, pending_reply=None, pending_update=None)
        logger.warning("summary %s: update reply refused: %s", day, result.get("error"))
    else:
        logger.warning("summary %s: update reply failed, retrying next tick: %s", day, result.get("error"))


def _lesson_changes(db, day: date, now: datetime, details: dict, lessons: dict, since: Optional[datetime]) -> tuple:
    """(left, joined): bullet lines «• HH:MM title — teacher: why» of lessons that left or joined the day.

    A summary kept before it remembered its lessons had them under the old rule — operational groups
    only — so its baseline is today's lessons without those kept only because they ran."""
    old = details.get("lessons")
    if old is None:
        kept = _kept_only_because_it_ran(db, [int(i) for i in lessons])
        old = {i: line for i, line in lessons.items() if int(i) not in kept}
    kept = _kept_only_because_it_ran(db, [int(i) for i in lessons if i not in old])
    left = tuple(f"{line}: {why_left(db, int(i), day, now)}" for i, line in old.items() if i not in lessons)
    joined = tuple(f"{line}: {why_joined(db, int(i), since, kept)}" for i, line in lessons.items() if i not in old)
    return left, joined


def refresh_sent(db, now: datetime, meet=None) -> int:
    """Re-read the summaries still live; edit and reply where they changed. Returns how many changed."""
    today = render.local(now).date()
    recent = (db.query(MeetStaffNotice)
              .filter(MeetStaffNotice.kind == "digest", MeetStaffNotice.status == "sent",
                      MeetStaffNotice.telegram_message_id.isnot(None),
                      MeetStaffNotice.day >= today - timedelta(days=1))
              .order_by(MeetStaffNotice.day).all())
    changed = 0
    for notice in recent:
        notice_id, day, message_id = notice.id, notice.day, notice.telegram_message_id
        details = dict(notice.details or {})
        if details.get("gone") or now >= _summary_start(day) + LIVE_FOR:
            continue
        _post_pending_reply(notice_id, db, day, message_id)
        details = dict(db.get(MeetStaffNotice, notice_id).details or {})
        if details.get("pending_reply"):
            continue  # say what changed before changing it again
        rendered_at = datetime.fromisoformat(details["rendered_at"]) if details.get("rendered_at") else None
        if (rendered_at is not None and now - rendered_at < REFRESH_EVERY
                and not _lessons_edited_since(db, day, rendered_at)):
            continue

        text = digest_text(db, day, now, meet) or (
            f"📋 <b>Meet — итоги дня, {day:%d.%m}</b>\nУроков в Meet: 0")
        lessons = lesson_lines(db, day, now)
        old = details.get("text")
        if old is None or old == text:
            # No baseline yet (sent before summaries were kept true), or nothing to say.
            _remember(db, notice_id, text=text, rendered_at=now, lessons=lessons)
            continue
        left, joined = _lesson_changes(db, day, now, details, lessons, rendered_at)
        group_id, _ = notices.target()
        edited = outbox.edit(group_id, message_id, f"{text}\n\n✏️ Обновлено {render.local(now):%d.%m в %H:%M}", None)
        if edited["gone"]:
            _remember(db, notice_id, gone=True)
            continue
        if not edited["ok"]:
            _remember(db, notice_id, rendered_at=now)  # try again in REFRESH_EVERY
            logger.warning("summary %s: edit failed: %s", day, edited.get("description"))
            continue
        reply = changes_text(day, old, text, left, joined)
        _remember(db, notice_id, text=text, rendered_at=now, lessons=lessons,
                  pending_reply=reply, pending_update=(details.get("updates", 0) + 1) if reply else None)
        if reply:
            _post_pending_reply(notice_id, db, day, message_id)
        changed += 1
        logger.info("summary %s refreshed (%s)", day, "reply sent" if reply else "edit only")
    return changed
