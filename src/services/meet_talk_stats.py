"""Talk time over many lessons: a group's students side by side, every teacher side by side,
one teacher's groups and lessons, and one student's lessons.

All of it is read lesson by lesson through ``meet_talk.compute`` — the same numbers the lesson
panel shows, added up. Only lessons with talk count: Meet's speaker timing, or for a lesson taught
before talk time was on, a transcript made afterwards whose voices stand in for it. Lessons are
read in chunks so a month of transcripts is never in memory at once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Iterator, Optional

from sqlalchemy import and_, exists, or_

from src.schemas.models import (
    Attendance,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    LessonTranscript,
    MeetSpeech,
    UserInDB,
)
from src.services import meet_presence, meet_talk, talk_settings
from src.utils.utc_json import utc_z

MAX_LESSONS = 200
MAX_TEACHER_LESSONS = 1000
CHUNK = 60
HEADS = frozenset({"admin", "head_curator", "head_teacher"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _has_talk():
    """Meet's speaker timing, or failing that a transcript whose voices stand in for it."""
    speech = exists().where(and_(MeetSpeech.event_id == Event.id, MeetSpeech.state == "saved")).correlate(Event)
    words = exists().where(and_(LessonTranscript.event_id == Event.id, LessonTranscript.status == "ready")).correlate(Event)
    return or_(speech, words)


def _talks(db, events: list, now: datetime) -> Iterator[tuple]:
    """(event, record, talk) for each lesson whose record is ready and that has talk, in order."""
    for i in range(0, len(events), CHUNK):
        chunk = events[i:i + CHUNK]
        records, batch = meet_presence.records_with_batch(db, chunk, now)
        ids = [e.id for e in chunk]
        speech = meet_talk.speech_by_event(db, ids)
        transcripts = meet_talk.transcripts_by_event(db, ids)
        for event, record in zip(chunk, records):
            rows = speech.get(event.id) or []
            if record.get("state") != "ready" or not meet_talk.has_talk(rows, transcripts.get(event.id)):
                continue
            yield event, record, meet_talk.compute(event, batch, record, rows,
                                                   transcript=transcripts.get(event.id), now=now)


def _group_names(db, event_ids: list) -> dict:
    names: dict = {}
    for event_id, gid, name in (db.query(EventGroup.event_id, Group.id, Group.name)
                                .join(Group, Group.id == EventGroup.group_id)
                                .filter(EventGroup.event_id.in_(event_ids or [-1]))):
        names.setdefault(event_id, []).append({"id": gid, "name": name})
    return names


def _questions(talk: dict) -> Optional[dict]:
    i = talk["insights"]
    return None if i is None else {"teacher_questions": i["teacher_questions"], "answered": i["answered"],
                                   "student_questions": i["student_questions"],
                                   "median_wait_seconds": i["median_wait_seconds"]}


class _Questions:
    """Questions added up over lessons that have a transcript; None when none do."""

    def __init__(self):
        self.lessons = self.teacher = self.answered = self.students = 0

    def add(self, talk: dict) -> None:
        q = _questions(talk)
        if q is not None:
            self.lessons += 1
            self.teacher += q["teacher_questions"]
            self.answered += q["answered"]
            self.students += q["student_questions"]

    def out(self) -> Optional[dict]:
        if not self.lessons:
            return None
        return {"teacher_questions": self.teacher, "answered": self.answered, "student_questions": self.students,
                "lessons_with_transcript": self.lessons}


def _lesson_row(event, record, talk: dict, groups: list) -> dict:
    teacher = record.get("teacher")
    q = _questions(talk) or {}
    return {
        "event_id": event.id, "title": event.title, "start": utc_z(event.start_datetime),
        "groups": groups,
        "teacher_name": teacher["name"] if teacher else None,
        "teacher_share": talk["teacher_share"], "students_share": talk["students_share"],
        "speech_seconds": talk["speech_seconds"],
        "longest_stretch_seconds": talk["longest_teacher_stretch_seconds"],
        "students_in_room": sum(1 for p in talk["people"] if p["role"] == "student" and p["in_room"]),
        "silent": len(talk["silent_students"]),
        "teacher_questions": q.get("teacher_questions"), "answered": q.get("answered"),
        "student_questions": q.get("student_questions"), "median_wait_seconds": q.get("median_wait_seconds"),
        "source": talk.get("source", "meet"),
    }


class _Tally:
    """One teacher's (or group's) lessons added up."""

    def __init__(self):
        self.lessons = 0
        self.teacher_shares, self.student_shares, self.stretches = [], [], []
        self.teacher_seconds = self.student_seconds = self.speech_seconds = 0
        self.silent = self.in_room = 0
        self.groups: set = set()
        self.questions = _Questions()

    def add(self, record, talk: dict, group_ids=()) -> None:
        self.lessons += 1
        if talk["teacher_share"] is not None:
            self.teacher_shares.append(talk["teacher_share"])
            self.student_shares.append(talk["students_share"])
        self.stretches.append(talk["longest_teacher_stretch_seconds"])
        self.teacher_seconds += talk["teacher_seconds"]
        self.student_seconds += sum(p["seconds"] for p in talk["people"] if p["role"] in ("student", "unknown"))
        self.speech_seconds += talk["speech_seconds"]
        self.silent += len(talk["silent_students"])
        self.in_room += sum(1 for p in talk["people"] if p["role"] == "student" and p["in_room"])
        self.groups.update(group_ids)
        self.questions.add(talk)

    def out(self) -> dict:
        n = self.lessons or 1
        return {
            "lessons": self.lessons,
            "groups": len(self.groups),
            "teacher_share": round(mean(self.teacher_shares), 3) if self.teacher_shares else None,
            "students_share": round(mean(self.student_shares), 3) if self.student_shares else None,
            "teacher_seconds": self.teacher_seconds,
            "student_seconds": self.student_seconds,
            "speech_seconds": self.speech_seconds,
            "longest_stretch_seconds": round(mean(self.stretches)) if self.stretches else 0,
            "silent_per_lesson": round(self.silent / n, 1),
            "students_in_room_per_lesson": round(self.in_room / n, 1),
            "questions": self.questions.out(),
        }


# ── one group ────────────────────────────────────────────────────────────────────────────

def may_see_group(db, viewer, group: Group) -> bool:
    if viewer.role in HEADS:
        return True
    if viewer.role == "teacher" and group.teacher_id == viewer.id:
        return True
    if viewer.role == "curator" and group.curator_id == viewer.id:
        return True
    in_group = exists().where(and_(EventGroup.event_id == Event.id, EventGroup.group_id == group.id)).correlate(Event)
    return db.query(Event.id).filter(meet_presence.visible_lessons_clause(viewer), in_group).first() is not None


def _student_state(person: dict, silent: set) -> str:
    if person["seconds"] > 0:
        return "spoke"
    if person["user_id"] in silent:
        return "silent"
    return "present" if person["in_room"] else "absent"


def group_talk(db, viewer, group: Group, date_from: datetime, date_to: datetime,
               now: Optional[datetime] = None) -> dict:
    """GET /meet-attendance/talk/groups/{id}: every student of the group, added up over the period."""
    now = now or _now()
    in_group = exists().where(and_(EventGroup.event_id == Event.id, EventGroup.group_id == group.id)).correlate(Event)
    events = (db.query(Event)
              .filter(meet_presence.visible_lessons_clause(viewer), in_group, _has_talk(),
                      Event.start_datetime >= date_from, Event.start_datetime < date_to)
              .order_by(Event.start_datetime.desc()).limit(MAX_LESSONS).all())
    names = _group_names(db, [e.id for e in events])

    students: dict = {}
    lessons, teacher_shares = [], []
    questions = _Questions()
    totals = {"lessons": 0, "speech_seconds": 0, "teacher_seconds": 0, "student_seconds": 0,
              "unconfirmed_seconds": 0}
    for event, record, talk in _talks(db, events, now):
        people = talk["people"]
        silent = {s["user_id"] for s in talk["silent_students"]}
        transcribed = talk["insights"] is not None
        for p in people:
            if p["role"] != "student":
                continue
            row = students.setdefault(p["user_id"], {
                "user_id": p["user_id"], "name": p["name"], "lessons_in_room": 0, "lessons_spoke": 0,
                "silent_lessons": 0, "total_seconds": 0, "questions": None, "answers": None, "lessons": []})
            row["lessons_in_room"] += 1 if p["in_room"] else 0
            row["lessons_spoke"] += 1 if p["seconds"] > 0 else 0
            row["silent_lessons"] += 1 if p["user_id"] in silent else 0
            row["total_seconds"] += p["seconds"]
            row["lessons"].append({"event_id": event.id, "start": utc_z(event.start_datetime),
                                   "state": _student_state(p, silent), "seconds": p["seconds"]})
            if transcribed:
                row["questions"] = (row["questions"] or 0) + (p["questions"] or 0)
                row["answers"] = (row["answers"] or 0) + (p["answers"] or 0)
        if talk["teacher_share"] is not None:
            teacher_shares.append(talk["teacher_share"])
        questions.add(talk)
        totals["lessons"] += 1
        totals["speech_seconds"] += talk["speech_seconds"]
        totals["teacher_seconds"] += talk["teacher_seconds"]
        totals["student_seconds"] += sum(p["seconds"] for p in people if p["role"] in ("student", "unknown"))
        totals["unconfirmed_seconds"] += sum(p["seconds"] for p in people if p["role"] == "unknown")
        lessons.append(_lesson_row(event, record, talk, names.get(event.id, [])))

    all_students = sum(r["total_seconds"] for r in students.values())
    rows = []
    for r in students.values():
        r["avg_seconds"] = round(r["total_seconds"] / r["lessons_in_room"]) if r["lessons_in_room"] else 0
        r["share_of_student_talk"] = round(r["total_seconds"] / all_students, 3) if all_students else 0.0
        r["lessons"].reverse()  # oldest first: the dots read left to right
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
        "questions": questions.out(),
    }


# ── every teacher, and one teacher ───────────────────────────────────────────────────────

def _taught(db, viewer, date_from: datetime, date_to: datetime, teacher_id: Optional[int] = None) -> list:
    query = db.query(Event).filter(meet_presence.visible_lessons_clause(viewer), _has_talk(),
                                   Event.teacher_id.isnot(None),
                                   Event.start_datetime >= date_from, Event.start_datetime < date_to)
    if teacher_id is not None:
        query = query.filter(Event.teacher_id == teacher_id)
    return query.order_by(Event.start_datetime.desc()).limit(MAX_TEACHER_LESSONS).all()


def teachers_talk(db, viewer, date_from: datetime, date_to: datetime, now: Optional[datetime] = None) -> dict:
    """GET /meet-attendance/talk/teachers: one row per teacher, over the period (heads)."""
    now = now or _now()
    events = _taught(db, viewer, date_from, date_to)
    names = _group_names(db, [e.id for e in events])
    tallies: dict = {}
    for event, record, talk in _talks(db, events, now):
        tallies.setdefault(event.teacher_id, _Tally()).add(record, talk, [g["id"] for g in names.get(event.id, [])])
    people = {u.id: u.name for u in db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(list(tallies) or [-1]))}
    rows = [{"teacher_id": tid, "name": people.get(tid) or f"User {tid}", **tally.out()} for tid, tally in tallies.items()]
    rows.sort(key=lambda r: (-(r["teacher_share"] or 0), r["name"].lower()))
    return {"from": utc_z(date_from), "to": utc_z(date_to), "teachers": rows}


def teacher_talk(db, viewer, teacher_id: int, date_from: datetime, date_to: datetime,
                 now: Optional[datetime] = None) -> Optional[dict]:
    """GET /meet-attendance/talk/teachers/{id}: one teacher's groups and lessons (heads)."""
    now = now or _now()
    teacher = db.get(UserInDB, teacher_id)
    if teacher is None:
        return None
    events = _taught(db, viewer, date_from, date_to, teacher_id)
    names = _group_names(db, [e.id for e in events])
    overall, by_group, lessons = _Tally(), {}, []
    for event, record, talk in _talks(db, events, now):
        groups = names.get(event.id, [])
        overall.add(record, talk, [g["id"] for g in groups])
        for g in groups or [{"id": None, "name": None}]:
            by_group.setdefault(g["id"], (g["name"], _Tally()))[1].add(record, talk)
        lessons.append(_lesson_row(event, record, talk, groups))
    group_rows = [{"group_id": gid, "name": name, **tally.out()} for gid, (name, tally) in by_group.items()]
    group_rows.sort(key=lambda r: (-r["lessons"], (r["name"] or "").lower()))
    return {
        "from": utc_z(date_from), "to": utc_z(date_to),
        "teacher": {"teacher_id": teacher.id, "name": teacher.name, **overall.out()},
        "groups": group_rows,
        "lessons": lessons,
    }


# ── one student ──────────────────────────────────────────────────────────────────────────

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
    names = _group_names(db, [e.id for e in events])

    lessons = []
    for event, _record, talk in _talks(db, events, now):
        me = next((p for p in talk["people"] if p["role"] == "student" and p["user_id"] == student_id), None)
        if me is None:
            continue  # not one of this lesson's students (taken off it)
        students = sum(p["seconds"] for p in talk["people"] if p["role"] in ("student", "unknown"))
        groups = names.get(event.id) or []
        lessons.append({"event_id": event.id, "start": utc_z(event.start_datetime), "title": event.title,
                        "group_name": groups[0]["name"] if groups else None, "seconds": me["seconds"],
                        "share_of_students": round(me["seconds"] / students, 3) if students else 0.0,
                        "in_room": me["in_room"], "questions": me["questions"], "answers": me["answers"]})
    in_room = [x for x in lessons if x["in_room"]]
    asked = [x["questions"] for x in lessons if x["questions"] is not None]
    answered = [x["answers"] for x in lessons if x["answers"] is not None]
    return {
        "lessons": lessons,
        "totals": {
            "lessons": len(lessons),
            "lessons_spoke": sum(1 for x in lessons if x["seconds"] > 0),
            "total_seconds": sum(x["seconds"] for x in lessons),
            "avg_seconds": round(sum(x["seconds"] for x in in_room) / len(in_room)) if in_room else 0,
            "questions": sum(asked) if asked else None,
            "answers": sum(answered) if answered else None,
        },
    }
