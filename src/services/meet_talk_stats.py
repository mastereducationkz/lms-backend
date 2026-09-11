"""Talk time over many lessons: one group's students side by side, and one student's lessons.

Both read lesson by lesson through ``meet_talk.compute`` — the same numbers the lesson panel
shows, added up. Only lessons with talk count: Meet's speaker timing, or for a lesson taught
before talk time was on, a transcript made afterwards whose voices stand in for it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Optional

from sqlalchemy import and_, exists, or_

from src.schemas.models import Attendance, Event, EventGroup, Group, GroupStudent, LessonTranscript, MeetSpeech
from src.services import meet_presence, meet_talk, talk_settings
from src.utils.utc_json import utc_z

MAX_LESSONS = 200
HEADS = frozenset({"admin", "head_curator", "head_teacher"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _has_talk():
    """Meet's speaker timing, or failing that a transcript whose voices stand in for it."""
    speech = exists().where(and_(MeetSpeech.event_id == Event.id, MeetSpeech.state == "saved")).correlate(Event)
    words = exists().where(and_(LessonTranscript.event_id == Event.id, LessonTranscript.status == "ready")).correlate(Event)
    return or_(speech, words)


def _talks(db, events: list, now: datetime) -> list:
    """(event, record, talk) for each lesson whose record is ready and whose speech is saved."""
    if not events:
        return []
    records, batch = meet_presence.records_with_batch(db, events, now)
    ids = [e.id for e in events]
    speech = meet_talk.speech_by_event(db, ids)
    transcripts = meet_talk.transcripts_by_event(db, ids)
    out = []
    for event, record in zip(events, records):
        rows = speech.get(event.id) or []
        if record.get("state") != "ready" or not meet_talk.has_talk(rows, transcripts.get(event.id)):
            continue
        out.append((event, record, meet_talk.compute(event, batch, record, rows,
                                                     transcript=transcripts.get(event.id), now=now)))
    return out


def may_see_group(db, viewer, group: Group) -> bool:
    if viewer.role in HEADS:
        return True
    if viewer.role == "teacher" and group.teacher_id == viewer.id:
        return True
    if viewer.role == "curator" and group.curator_id == viewer.id:
        return True
    in_group = exists().where(and_(EventGroup.event_id == Event.id, EventGroup.group_id == group.id)).correlate(Event)
    return db.query(Event.id).filter(meet_presence.visible_lessons_clause(viewer), in_group).first() is not None


def group_talk(db, viewer, group: Group, date_from: datetime, date_to: datetime,
               now: Optional[datetime] = None) -> dict:
    """GET /meet-attendance/talk/groups/{id}: every student of the group, added up over the period."""
    now = now or _now()
    in_group = exists().where(and_(EventGroup.event_id == Event.id, EventGroup.group_id == group.id)).correlate(Event)
    events = (db.query(Event)
              .filter(meet_presence.visible_lessons_clause(viewer), in_group, _has_talk(),
                      Event.start_datetime >= date_from, Event.start_datetime < date_to)
              .order_by(Event.start_datetime.desc()).limit(MAX_LESSONS).all())
    talks = _talks(db, events, now)

    students: dict = {}
    lessons, teacher_shares = [], []
    totals = {"lessons": len(talks), "speech_seconds": 0, "teacher_seconds": 0, "student_seconds": 0,
              "unconfirmed_seconds": 0}
    for event, record, talk in talks:
        people = talk["people"]
        silent = {s["user_id"] for s in talk["silent_students"]}
        transcribed = talk["insights"] is not None
        for p in people:
            if p["role"] != "student":
                continue
            row = students.setdefault(p["user_id"], {
                "user_id": p["user_id"], "name": p["name"], "lessons_in_room": 0, "lessons_spoke": 0,
                "silent_lessons": 0, "total_seconds": 0, "questions": None})
            row["lessons_in_room"] += 1 if p["in_room"] else 0
            row["lessons_spoke"] += 1 if p["seconds"] > 0 else 0
            row["silent_lessons"] += 1 if p["user_id"] in silent else 0
            row["total_seconds"] += p["seconds"]
            if transcribed:
                row["questions"] = (row["questions"] or 0) + (p["questions"] or 0)
        if talk["teacher_share"] is not None:
            teacher_shares.append(talk["teacher_share"])
        student_seconds = sum(p["seconds"] for p in people if p["role"] in ("student", "unknown"))
        totals["speech_seconds"] += talk["speech_seconds"]
        totals["teacher_seconds"] += talk["teacher_seconds"]
        totals["student_seconds"] += student_seconds
        totals["unconfirmed_seconds"] += sum(p["seconds"] for p in people if p["role"] == "unknown")
        teacher = record.get("teacher")
        lessons.append({
            "event_id": event.id, "title": event.title, "start": utc_z(event.start_datetime),
            "teacher_name": teacher["name"] if teacher else None,
            "teacher_share": talk["teacher_share"], "students_share": talk["students_share"],
            "speech_seconds": talk["speech_seconds"],
            "students_in_room": sum(1 for p in people if p["role"] == "student" and p["in_room"]),
            "silent": len(silent),
        })

    all_students = sum(r["total_seconds"] for r in students.values())
    rows = []
    for r in students.values():
        r["avg_seconds"] = round(r["total_seconds"] / r["lessons_in_room"]) if r["lessons_in_room"] else 0
        r["share_of_student_talk"] = round(r["total_seconds"] / all_students, 3) if all_students else 0.0
        rows.append(r)
    rows.sort(key=lambda r: (-r["total_seconds"], r["name"].lower()))
    return {
        "group": {"id": group.id, "name": group.name},
        "from": utc_z(date_from), "to": utc_z(date_to),
        "lessons": lessons,
        "students": rows,
        "teacher": {"avg_share": round(mean(teacher_shares), 3) if teacher_shares else None,
                    "lessons": len(teacher_shares)},
        "totals": totals,
    }


def student_talk(db, student_id: int, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                 now: Optional[datetime] = None, limit: int = MAX_LESSONS) -> Optional[dict]:
    """One student's talk lesson by lesson, for the student report. None while talk time is off."""
    if not talk_settings.enabled(db):
        return None
    now = now or _now()
    date_to = date_to or now
    date_from = date_from or date_to - timedelta(days=120)
    in_their_group = exists().where(and_(
        EventGroup.event_id == Event.id,
        EventGroup.group_id.in_(db.query(GroupStudent.group_id).filter(GroupStudent.student_id == student_id)),
    )).correlate(Event)
    marked = exists().where(and_(Attendance.event_id == Event.id, Attendance.user_id == student_id)).correlate(Event)
    events = (db.query(Event)
              .filter(or_(in_their_group, marked), _has_talk(),
                      Event.start_datetime >= date_from, Event.start_datetime < date_to)
              .order_by(Event.start_datetime.desc()).limit(limit).all())
    group_names = {}
    for event_id, name in (db.query(EventGroup.event_id, Group.name).join(Group, Group.id == EventGroup.group_id)
                           .filter(EventGroup.event_id.in_([e.id for e in events] or [-1]))):
        group_names.setdefault(event_id, name)

    lessons = []
    for event, _record, talk in _talks(db, events, now):
        me = next((p for p in talk["people"] if p["role"] == "student" and p["user_id"] == student_id), None)
        if me is None:
            continue  # not one of this lesson's students (taken off it)
        students = sum(p["seconds"] for p in talk["people"] if p["role"] in ("student", "unknown"))
        lessons.append({"event_id": event.id, "start": utc_z(event.start_datetime), "title": event.title,
                        "group_name": group_names.get(event.id), "seconds": me["seconds"],
                        "share_of_students": round(me["seconds"] / students, 3) if students else 0.0,
                        "in_room": me["in_room"], "questions": me["questions"]})
    in_room = [x for x in lessons if x["in_room"]]
    asked = [x["questions"] for x in lessons if x["questions"] is not None]
    return {
        "lessons": lessons,
        "totals": {
            "lessons": len(lessons),
            "lessons_spoke": sum(1 for x in lessons if x["seconds"] > 0),
            "total_seconds": sum(x["seconds"] for x in lessons),
            "avg_seconds": round(sum(x["seconds"] for x in in_room) / len(in_room)) if in_room else 0,
            "questions": sum(asked) if asked else None,
        },
    }
