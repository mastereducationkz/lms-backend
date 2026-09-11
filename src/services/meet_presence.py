"""What a lesson's Meet record means: who was there, from when to when, and which marks
disagree with it.

``meet_attendance`` stores what Meet reported — Google accounts and sessions. This module
reads it back as people. Nothing here is stored: who an account belongs to is looked up at
read time (``GoogleAccountLink``), so confirming an account once corrects every lesson it
appears in, past and future.

The record is **evidence, never a mark** (owner, 2026-09-11). Marks feed CRM billing and
stay the teacher's. What this adds is flags where the two disagree, plus lateness — and one
safety rule: while an unconfirmed account was in the room, "never joined" is held back,
because that account may well be the student.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

from sqlalchemy import false
from sqlalchemy.orm import selectinload

from src.schemas.models import (
    Attendance,
    Event,
    EventGroup,
    GoogleAccountLink,
    Group,
    GroupStudent,
    MeetConference,
    MeetFlagReview,
    MeetParticipant,
    UserInDB,
)
from src.services.attendance_status import ABSENT_STATUSES, PRESENT_STATUSES, REMOVED_STATUSES
from src.services.meet_recordings import meet_code
from src.services.recording_access import watchable_event_clause
from src.utils.utc_json import utc_z

# The rules approved by the owner, 2026-09-11.
STUDENT_LATE_AFTER = timedelta(minutes=5)
STUDENT_LEFT_EARLY_BEFORE = timedelta(minutes=10)
ABSENT_BUT_IN_ROOM_FOR = timedelta(minutes=10)
TEACHER_LATE_AFTER = timedelta(minutes=2)
TEACHER_ENDED_EARLY_BEFORE = timedelta(minutes=5)

# A room exists days before its lesson, so not every visit is the lesson: 14156 had two
# morning test calls. Time in the room this far either side of the lesson counts; the rest
# is another visit and is left out.
LESSON_MARGIN = timedelta(minutes=30)

# The record is complete once the lesson is this far behind us (the worker ticks every 5 min
# and reads a call 5 min after it ends). Before that nothing is judged — a teacher who has not
# joined *yet* is not a teacher who never joined.
COMPLETE_AFTER = timedelta(minutes=20)
# A call Google will not hand over does not hide the rest of the lesson for ever.
GIVE_UP_WAITING_AFTER = timedelta(hours=6)
# Google keeps conference records about this long; an older lesson with nothing saved simply
# predates the record, which is not the same as nobody coming.
GOOGLE_KEEPS = timedelta(days=30)

MISMATCH_CODES = frozenset({"marked_present_not_joined", "marked_absent_was_in_room", "teacher_not_joined"})

# ── reviewing a flag (owner, 2026-09-11) ─────────────────────────────────────────────────
# A flag looked at by a person, with the reason, stops asking. A reason is required where the
# mark contradicts the room, optional for lateness. «Другое» (free text) is always offered.
_TIMING = [("warned", "Предупредил заранее"), ("tech", "Технические проблемы"), ("valid", "Уважительная причина")]
_TEACHER_TIMING = [("tech", "Технические проблемы"), ("agreed", "Согласовано с руководством"),
                   ("moved", "Урок перенесён или продлён")]
REVIEW_REASONS = {
    "marked_present_not_joined": [("excused", "Отпросился"), ("other_device", "С другого аккаунта или устройства"),
                                  ("outside_meet", "Занимался вне Meet")],
    "marked_absent_was_in_room": [("not_participating", "Был, но не участвовал")],
    "late": _TIMING,
    "left_early": _TIMING,
    "teacher_late": _TEACHER_TIMING,
    "ended_early": _TEACHER_TIMING,
    "teacher_not_joined": [("substitute", "Урок провёл другой преподаватель"), ("moved", "Урок перенесён или отменён"),
                           ("tech", "Технические проблемы")],
}
OTHER_REASON = ("other", "Другое")
REASON_REQUIRED = frozenset({"marked_present_not_joined", "marked_absent_was_in_room", "teacher_not_joined"})
TEACHER_FLAGS = frozenset({"teacher_late", "ended_early", "teacher_not_joined"})
# A teacher's own flags are cleared by admins and heads, never by the teacher (owner, 2026-09-11).
TEACHER_FLAG_REVIEWERS = frozenset({"admin", "head_curator", "head_teacher"})


def reason_label(code: str, reason_code: Optional[str]) -> Optional[str]:
    if not reason_code:
        return None
    return dict(REVIEW_REASONS.get(code, []) + [OTHER_REASON]).get(reason_code)


def review_options() -> dict:
    """What a review form offers, per flag: the reasons, and whether one is required."""
    return {code: {"required": code in REASON_REQUIRED,
                   "reasons": [{"key": k, "label": l} for k, l in reasons + [OTHER_REASON]]}
            for code, reasons in REVIEW_REASONS.items()}

# Who reads the record: the Recordings rule, without students (owner, 2026-09-11).
RECORD_ROLES = frozenset({"admin", "head_curator", "head_teacher", "teacher", "curator"})


def visible_lessons_clause(user):
    """SQL: the lessons whose Meet record ``user`` may read (and whose accounts they may confirm)."""
    if getattr(user, "role", None) not in RECORD_ROLES:
        return false()
    return watchable_event_clause(user)


# ── names ────────────────────────────────────────────────────────────────────────────────
# Meet shows the Google account's name — often Latin where the LMS has Cyrillic, often a
# nickname. Both sides are folded to one plain Latin spelling before comparing, so «Аяулым»
# and "Ayaulym", «Шыңғыс» and "Shyngys", «Шерхан» and "Sherkhan" meet in the middle.

_TO_LATIN = str.maketrans({
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "j", "з": "z", "и": "i", "й": "i", "к": "k", "қ": "k", "л": "l", "м": "m", "н": "n",
    "ң": "n", "о": "o", "ө": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u",
    "ү": "u", "ф": "f", "х": "h", "һ": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "",
    "ы": "i", "і": "i", "ь": "", "э": "e", "ю": "iu", "я": "ia",
})
# Latin spellings of the same sound, folded the same way as the Cyrillic above.
_LATIN_FOLDS = (("zh", "j"), ("kh", "h"), ("ts", "c"), ("y", "i"), ("q", "k"), ("w", "u"), ("x", "h"))

SUGGEST_AT = 0.8


def name_words(name: Optional[str]) -> list:
    text = (name or "").lower().translate(_TO_LATIN)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    for spelling, folded in _LATIN_FOLDS:
        text = text.replace(spelling, folded)
    text = re.sub(r"(.)\1+", r"\1", text)
    return [w for w in re.split(r"[^a-z]+", text) if len(w) >= 2]


def _word_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return 0.9  # "Aya" for «Аяулым»
    return SequenceMatcher(None, a, b).ratio()


def name_score(display_name: Optional[str], lms_name: Optional[str]) -> float:
    """How well a Meet display name fits an LMS name: the best-fitting word, plus a little
    when a second word (the surname) fits too."""
    shown, known = name_words(display_name), name_words(lms_name)
    if not shown or not known:
        return 0.0
    best = sorted((max(_word_score(w, k) for k in known) for w in shown), reverse=True)
    return best[0] + (0.05 * best[1] if len(best) > 1 and best[1] >= SUGGEST_AT else 0.0)


def suggest(display_name: Optional[str], candidates: list) -> Optional[dict]:
    """The one candidate the name clearly points to, or None when none does — or two do
    equally well (two «Аружан» in a group is a choice for a person, not for us)."""
    ranked = sorted(((name_score(display_name, c["name"]), c) for c in candidates),
                    key=lambda pair: pair[0], reverse=True)
    if not ranked or ranked[0][0] < SUGGEST_AT:
        return None
    if len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.02:
        return None
    return {"user_id": ranked[0][1]["user_id"], "name": ranked[0][1]["name"]}


# ── time ─────────────────────────────────────────────────────────────────────────────────

def _minutes(delta: timedelta) -> int:
    return max(1, math.ceil(delta.total_seconds() / 60))


def merge_spans(spans: list) -> list:
    """Overlapping stretches become one (two devices, or a reconnect before the old one closed)."""
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def time_inside(spans: list, lo: datetime, hi: datetime) -> timedelta:
    total = timedelta(0)
    for a, b in merge_spans(spans):
        a, b = max(a, lo), min(b, hi)
        if b > a:
            total += b - a
    return total


def student_flags(start: datetime, end: datetime, spans: list, mark: Optional[str], *,
                  unconfirmed_in_room: bool) -> list:
    flags = []
    if spans:
        first, last = min(s[0] for s in spans), max(s[1] for s in spans)
        if first > start + STUDENT_LATE_AFTER:
            flags.append({"code": "late", "minutes": _minutes(first - start)})
        if last < end - STUDENT_LEFT_EARLY_BEFORE:
            flags.append({"code": "left_early", "minutes": _minutes(end - last)})
    if mark in ("present", "late") and not spans and not unconfirmed_in_room:
        flags.append({"code": "marked_present_not_joined"})
    inside = time_inside(spans, start, end)
    if mark == "absent" and inside >= ABSENT_BUT_IN_ROOM_FOR:
        flags.append({"code": "marked_absent_was_in_room", "minutes": int(inside.total_seconds() // 60)})
    return flags


def teacher_flags(start: datetime, end: datetime, spans: list, *, unconfirmed_in_room: bool) -> list:
    if not spans:
        return [] if unconfirmed_in_room else [{"code": "teacher_not_joined"}]
    flags = []
    first, last = min(s[0] for s in spans), max(s[1] for s in spans)
    if first > start + TEACHER_LATE_AFTER:
        flags.append({"code": "teacher_late", "minutes": _minutes(first - start)})
    if last < end - TEACHER_ENDED_EARLY_BEFORE:
        flags.append({"code": "ended_early", "minutes": _minutes(end - last)})
    return flags


def mark_of(status: Optional[str]) -> Optional[str]:
    """The stored status in the four words the record speaks (see attendance_status)."""
    s = (status or "").strip().lower()
    if s == "late":
        return "late"
    if s in PRESENT_STATUSES:
        return "present"
    if s in ABSENT_STATUSES:
        return "absent"
    if s in REMOVED_STATUSES:
        return "removed"
    return None


# ── loading ──────────────────────────────────────────────────────────────────────────────

class _Batch:
    """Everything the records of a set of lessons need, in a fixed handful of queries."""

    def __init__(self, db, events: list):
        ids = [e.id for e in events] or [-1]
        self.conferences: dict = {}
        for c in db.query(MeetConference).filter(MeetConference.event_id.in_(ids)):
            self.conferences.setdefault(c.event_id, []).append(c)
        self.participants: dict = {}
        for p in (db.query(MeetParticipant).options(selectinload(MeetParticipant.sessions))
                  .filter(MeetParticipant.event_id.in_(ids))):
            self.participants.setdefault(p.event_id, []).append(p)
        accounts = {p.google_user for ps in self.participants.values() for p in ps if p.google_user}
        self.links = {link.google_user: link for link in
                      db.query(GoogleAccountLink).filter(GoogleAccountLink.google_user.in_(accounts or [""]))}
        self.roster: dict = {}
        for event_id, student_id, enrolled in (
            db.query(EventGroup.event_id, GroupStudent.student_id, GroupStudent.created_at)
            .join(GroupStudent, GroupStudent.group_id == EventGroup.group_id)
            .filter(EventGroup.event_id.in_(ids))
        ):
            self.roster.setdefault(event_id, {})[student_id] = enrolled
        self.marks: dict = {}
        for event_id, user_id, status in (db.query(Attendance.event_id, Attendance.user_id, Attendance.status)
                                          .filter(Attendance.event_id.in_(ids))):
            self.marks.setdefault(event_id, {})[user_id] = mark_of(status)
        self.reviews = {(r.event_id, r.user_id, r.code): r
                        for r in db.query(MeetFlagReview).filter(MeetFlagReview.event_id.in_(ids))}
        user_ids = ({e.teacher_id for e in events if e.teacher_id}
                    | {r.reviewed_by for r in self.reviews.values() if r.reviewed_by}
                    | {u for r in self.roster.values() for u in r}
                    | {u for m in self.marks.values() for u in m}
                    | {link.user_id for link in self.links.values() if link.user_id}
                    | {p.lesson_user_id for ps in self.participants.values() for p in ps if p.lesson_user_id})
        self.users = {u.id: u for u in
                      db.query(UserInDB.id, UserInDB.name, UserInDB.role).filter(UserInDB.id.in_(user_ids or [-1]))}
        # Lessons held in one of our rooms: taught by, or in a group owned by, a teacher with a
        # Workspace account — the rule the recordings pipeline and its missing-recording alert
        # use. Anything else had a teacher's own Meet link, which the LMS cannot read.
        self.lms_rooms = {eid for (eid,) in (db.query(Event.id).join(UserInDB, UserInDB.id == Event.teacher_id)
                                            .filter(Event.id.in_(ids), UserInDB.workspace_email.isnot(None)))}
        self.lms_rooms |= {eid for (eid,) in (db.query(EventGroup.event_id)
                                             .join(Group, Group.id == EventGroup.group_id)
                                             .join(UserInDB, UserInDB.id == Group.teacher_id)
                                             .filter(EventGroup.event_id.in_(ids), UserInDB.workspace_email.isnot(None)))}

    def resolve(self, participant) -> tuple:
        """(user_id, hidden) for one Meet participant; (None, False) while nobody has said."""
        if participant.kind == "signed_in" and participant.google_user:
            link = self.links.get(participant.google_user)
            if link is None:
                return None, False
            return link.user_id, link.not_a_student
        return participant.lesson_user_id, bool(participant.lesson_not_a_student)


def _spans_json(spans: list) -> list:
    return [{"joined_at": utc_z(a), "left_at": utc_z(b)} for a, b in merge_spans(spans)]


def _presence(spans: list, start: datetime, end: datetime) -> dict:
    return {
        "sessions": _spans_json(spans),
        "joins": len(spans),
        "first_join": utc_z(min(s[0] for s in spans)) if spans else None,
        "last_leave": utc_z(max(s[1] for s in spans)) if spans else None,
        "minutes_in_lesson": int(time_inside(spans, start, end).total_seconds() // 60),
    }


def _student_ids(event, batch: _Batch, end: datetime) -> set:
    """The lesson's students: enrolled by the time it ended, plus anyone with a mark on it —
    minus anyone taken off it, and never its own teacher."""
    marks = batch.marks.get(event.id, {})
    enrolled = batch.roster.get(event.id, {})
    ids = ({uid for uid, since in enrolled.items() if since is None or since <= end}
           | {uid for uid, mark in marks.items() if mark is not None})
    ids -= {uid for uid, mark in marks.items() if mark == "removed"}
    ids.discard(event.teacher_id)
    return ids


def _roster(event, batch: _Batch, end: datetime) -> list:
    """The whole class list with each student's mark — what there is to show without Meet data."""
    marks = batch.marks.get(event.id, {})
    rows = [{"user_id": uid, "name": batch.users[uid].name if uid in batch.users else f"User {uid}",
             "mark": marks.get(uid)} for uid in _student_ids(event, batch, end)]
    return sorted(rows, key=lambda r: r["name"].lower())


def lesson_record(event, batch: _Batch, now: datetime) -> dict:
    start = event.start_datetime
    end = event.end_datetime or start + timedelta(hours=1)
    conferences = batch.conferences.get(event.id, [])
    base = {"event_id": event.id, "title": event.title, "start": utc_z(start), "end": utc_z(end)}
    # Without a Meet record there is still a class to list: who was expected, and their marks.
    unjudged = {**base, "roster": _roster(event, batch, end)}

    waiting_for_google = any(c.synced_at is None for c in conferences)
    if now < start:
        return {**unjudged, "state": "not_started"}
    if not conferences and (event.id not in batch.lms_rooms or not meet_code(event.meeting_url)):
        return {**unjudged, "state": "no_room"}  # nothing we could ever have read
    if now < end + COMPLETE_AFTER or (waiting_for_google and now < end + GIVE_UP_WAITING_AFTER):
        return {**unjudged, "state": "waiting"}
    if not any(c.synced_at for c in conferences):
        return {**unjudged, "state": "unavailable" if now > end + GOOGLE_KEEPS else "none"}

    lo, hi = start - LESSON_MARGIN, end + LESSON_MARGIN
    ended_at = {c.id: c.ended_at or now for c in conferences}
    spans_of: dict = {}   # user_id -> spans
    accounts_of: dict = {}  # user_id -> account summaries
    unknown, not_tracked = [], []
    for p in batch.participants.get(event.id, []):
        spans = [(s.joined_at, s.left_at or ended_at.get(p.conference_id, now)) for s in p.sessions]
        spans = [(a, b) for a, b in spans if b > lo and a < hi]
        if not spans:
            continue  # another visit to the room, not this lesson
        user_id, hidden = batch.resolve(p)
        account = {"participant_id": p.id, "kind": p.kind, "display_name": p.display_name}
        if hidden:
            not_tracked.append({**account, **_presence(spans, start, end)})
        elif user_id is None:
            unknown.append({**account, "_spans": spans})
        else:
            spans_of.setdefault(user_id, []).extend(spans)
            accounts_of.setdefault(user_id, []).append(account)

    marks = batch.marks.get(event.id, {})
    student_ids = _student_ids(event, batch, end)
    unconfirmed = bool(unknown)

    def person(uid: int, role: str, flags: list) -> dict:
        user = batch.users.get(uid)
        return {"user_id": uid, "name": user.name if user else f"User {uid}", "role": role,
                "mark": marks.get(uid), "accounts": accounts_of.get(uid, []),
                **_presence(spans_of.get(uid, []), start, end), "flags": flags}

    teacher = None
    if event.teacher_id:
        teacher = person(event.teacher_id, "teacher",
                         teacher_flags(start, end, spans_of.get(event.teacher_id, []), unconfirmed_in_room=unconfirmed))
    students = sorted(
        (person(uid, "student", student_flags(start, end, spans_of.get(uid, []), marks.get(uid),
                                              unconfirmed_in_room=unconfirmed))
         for uid in student_ids),
        key=lambda p: p["name"].lower())
    others = sorted(
        (person(uid, getattr(batch.users.get(uid), "role", None) or "other", [])
         for uid in spans_of if uid not in student_ids and uid != event.teacher_id),
        key=lambda p: p["name"].lower())

    candidates = ([{"user_id": teacher["user_id"], "name": teacher["name"], "role": "teacher"}] if teacher else []) + \
        [{"user_id": s["user_id"], "name": s["name"], "role": "student"} for s in students]
    unknown_out = [
        {**{k: v for k, v in u.items() if k != "_spans"}, **_presence(u["_spans"], start, end),
         "suggestion": suggest(u["display_name"], candidates)}
        for u in sorted(unknown, key=lambda u: min(s[0] for s in u["_spans"]))
    ]

    def review_of(uid: int, code: str) -> Optional[dict]:
        r = batch.reviews.get((event.id, uid, code))
        if r is None:
            return None
        by = batch.users.get(r.reviewed_by) if r.reviewed_by else None
        return {"reason_code": r.reason_code, "reason_label": reason_label(code, r.reason_code),
                "text": r.reason_text, "by": by.name if by else None, "at": utc_z(r.reviewed_at)}

    for p in ([teacher] if teacher else []) + students:
        for f in p["flags"]:
            f["review"] = review_of(p["user_id"], f["code"])

    flags = [{**f, "user_id": p["user_id"], "name": p["name"], "role": p["role"]}
             for p in ([teacher] if teacher else []) + students for f in p["flags"]]
    return {
        **base,
        "state": "ready",
        "partial": waiting_for_google,
        "calls": [{"started_at": utc_z(c.started_at), "ended_at": utc_z(c.ended_at)}
                  for c in sorted(conferences, key=lambda c: c.started_at or start)],
        "teacher": teacher,
        "students": students,
        "others": others,
        "unknown": unknown_out,
        "not_tracked": not_tracked,
        "held_back": unconfirmed,
        "flags": flags,
        # Open ones only: a reviewed flag has been answered and stops asking for attention.
        "mismatches": sum(1 for f in flags if f["code"] in MISMATCH_CODES and not f.get("review")),
        "reviewed": sum(1 for f in flags if f.get("review")),
        "review_options": review_options(),
        "candidates": sorted(candidates, key=lambda c: (c["role"] != "teacher", c["name"].lower())),
    }


def records(db, events: list, now: Optional[datetime] = None) -> list:
    return records_with_batch(db, events, now)[0]


def records_with_batch(db, events: list, now: Optional[datetime] = None) -> tuple:
    """The records, and the batch they were read from — talk time names speech with it."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    batch = _Batch(db, events)
    return [lesson_record(e, batch, now) for e in events], batch


def lesson(db, event: Event, now: Optional[datetime] = None) -> dict:
    return records(db, [event], now)[0]


def candidate_ids(db, event: Event) -> set:
    """Whom an account in this lesson may be confirmed as: its teacher, and its students —
    the groups' rosters plus anyone with a mark on the lesson."""
    ids = {event.teacher_id} if event.teacher_id else set()
    ids |= {sid for (sid,) in db.query(GroupStudent.student_id)
            .join(EventGroup, EventGroup.group_id == GroupStudent.group_id)
            .filter(EventGroup.event_id == event.id)}
    ids |= {uid for (uid,) in db.query(Attendance.user_id).filter(Attendance.event_id == event.id)}
    return ids


def public_participants(record: dict) -> dict:
    """What a page outside the LMS may show about who was there: names, marks, times, flags.

    For the watch-link page (accountants, no LMS account). Nothing that lets anyone act — no
    account ids, no "who is this" candidates, no Google identities — only what was read.
    Without a Meet record it is still the whole class list with marks.
    """
    def presence(p: dict) -> dict:
        return {"first_join": p.get("first_join"), "last_leave": p.get("last_leave"),
                "minutes_in_lesson": p.get("minutes_in_lesson", 0), "joins": p.get("joins", 0)}

    def flag(f: dict) -> dict:
        review = f.get("review")
        return {"code": f["code"], "minutes": f.get("minutes"),
                "review": {"reason_label": review.get("reason_label"), "text": review.get("text")} if review else None}

    def person(p: dict) -> dict:
        return {"name": p["name"], "mark": p.get("mark"), **presence(p), "flags": [flag(f) for f in p.get("flags", [])]}

    if record.get("state") != "ready":
        return {
            "state": record.get("state"),
            "teacher": None,
            "students": [{"name": r["name"], "mark": r["mark"], "first_join": None, "last_leave": None,
                          "minutes_in_lesson": 0, "joins": 0, "flags": []} for r in record.get("roster", [])],
            "unknown": [], "others": [], "held_back": False, "partial": False,
        }
    teacher = record.get("teacher")
    return {
        "state": "ready",
        "teacher": person(teacher) if teacher else None,
        "students": [person(s) for s in record.get("students", [])],
        "unknown": [{"display_name": u.get("display_name"), "kind": u.get("kind"), **presence(u)}
                    for u in record.get("unknown", [])],
        "others": [{**person(o), "role": o.get("role")} for o in record.get("others", [])],
        "held_back": bool(record.get("held_back")),
        "partial": bool(record.get("partial")),
    }
