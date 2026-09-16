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

Since 2026-09-16 each student also gets **Meet's verdict** — present, late or absent under the
rules Meet will one day take the register by (``lesson_clock`` and ``verdict``). It sits beside
the mark; a disagreement that changes billing asks for attention, and only a person applies it.
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
TEACHER_LATE_AFTER = timedelta(minutes=2)
TEACHER_ENDED_EARLY_BEFORE = timedelta(minutes=5)

# Meet's verdict (owner, 2026-09-16): late after more than 5 minutes, absent under 75% of the
# lesson actually held. Whole minutes, every rounding in the student's favour — 5:59 late is on
# time, 44:01 of a 60-minute lesson is the 45 needed. The same clock times the student flags, so
# a «late» flag and a «late» verdict can never disagree.
VERDICT_LATE_AFTER_MINUTES = 5
VERDICT_PRESENT_SHARE = 0.75
STUDENT_LEFT_EARLY_AFTER_MINUTES = 10
# Less teaching than this inside the lesson is not a lesson to follow: the timetable decides.
TEACHER_CLOCK_MIN_TAUGHT = timedelta(minutes=5)

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
# A lesson is judged only once a saved call overlaps the lesson itself by this much. Rooms are
# opened before the lesson — a teacher checks the link, a student arrives early — and those short
# calls are saved first. On 2026-09-15 the worker stalled right after saving them, and five
# lessons that were taught and recorded showed "Teacher never joined" and every present mark as
# "never joined". Not the 30-minute margin: an 18:58 five-second check sits inside it, and one
# check call ran 1.5 s past the start.
LESSON_CALL_MIN_OVERLAP = timedelta(minutes=5)
# Google keeps conference records about this long; an older lesson with nothing saved simply
# predates the record, which is not the same as nobody coming.
GOOGLE_KEEPS = timedelta(days=30)

MISMATCH_CODES = frozenset({"marked_present_not_joined", "marked_present_too_short", "marked_absent_was_in_room",
                            "teacher_not_joined"})
# Flags read off Meet's verdict. The watch page (accountants) gets no verdicts (owner, 2026-09-16).
VERDICT_ONLY_CODES = frozenset({"marked_present_too_short"})

# ── reviewing a flag (owner, 2026-09-11) ─────────────────────────────────────────────────
# A flag looked at by a person, with the reason, stops asking. A reason is required where the
# mark contradicts the room, optional for lateness. «Другое» (free text) is always offered.
_TIMING = [("warned", "Предупредил заранее"), ("tech", "Технические проблемы"), ("valid", "Уважительная причина")]
_TEACHER_TIMING = [("tech", "Технические проблемы"), ("agreed", "Согласовано с руководством"),
                   ("moved", "Урок перенесён или продлён")]
REVIEW_REASONS = {
    "marked_present_not_joined": [("excused", "Отпросился"), ("other_device", "С другого аккаунта или устройства"),
                                  ("outside_meet", "Занимался вне Meet")],
    "marked_present_too_short": [("excused_early", "Отпросился раньше"), ("connection", "Проблемы со связью"),
                                 ("other_device", "С другого аккаунта или устройства")],
    "marked_absent_was_in_room": [("not_participating", "Был, но не участвовал")],
    "late": _TIMING,
    "left_early": _TIMING,
    "teacher_late": _TEACHER_TIMING,
    "ended_early": _TEACHER_TIMING,
    "teacher_not_joined": [("substitute", "Урок провёл другой преподаватель"), ("moved", "Урок перенесён или отменён"),
                           ("tech", "Технические проблемы")],
}
OTHER_REASON = ("other", "Другое")
REASON_REQUIRED = frozenset({"marked_present_not_joined", "marked_present_too_short", "marked_absent_was_in_room",
                             "teacher_not_joined"})
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


def _whole_minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def lesson_clock(start: datetime, end: datetime, teacher_spans: list) -> dict:
    """The stretch students are judged by: the timetable, narrowed to the teacher (owner, 2026-09-16).

    A late teacher moves the start and one who ended early moves the end, so nobody is late or
    absent for the teacher. The time needed is 75% of what is left, in whole minutes rounded down.
    Minutes after the scheduled end while the teacher still taught count for the students (up to
    ``LESSON_MARGIN``) but never raise the time needed. A teacher not in the lesson for at least
    ``TEACHER_CLOCK_MIN_TAUGHT`` — not seen, a room check, an unconfirmed account — leaves the timetable.
    """
    taught = [(a, b) for a, b in merge_spans(teacher_spans) if b > start and a < end]
    follows_teacher = time_inside(taught, start, end) >= TEACHER_CLOCK_MIN_TAUGHT
    if follows_teacher:
        lo, hi = max(start, taught[0][0]), min(end, taught[-1][1])
        until = max(hi, min(taught[-1][1], end + LESSON_MARGIN))
    else:
        lo, hi, until = start, end, end
    held = _whole_minutes(hi - lo)
    return {"start": lo, "end": hi, "count_until": until, "held_minutes": held,
            # 0.75 × whole minutes is exact in binary; the epsilon only guards a future share.
            "required_minutes": int(held * VERDICT_PRESENT_SHARE + 1e-9), "follows_teacher": follows_teacher}


def _stays(clock: dict, spans: list) -> list:
    """The student's merged stretches in the room that reach into the lesson — a room check that
    ended before the lesson began is not an arrival."""
    lo, until = clock["start"], clock["count_until"]
    return [(a, b) for a, b in merge_spans(spans) if b > lo and a < until]


def verdict(clock: dict, spans: list, *, unknown_in_room: bool) -> dict:
    """Meet's verdict on one student: ``present``, ``late`` or ``absent`` — or None, held back.

    Absent beats late. An unconfirmed account in the room can only add time and an earlier
    arrival, so while one is there «absent» and «late» wait for it and «present» does not; if the
    teacher was not seen either, that account may be the teacher, the clock is unknown, and
    nothing is judged. ``attended`` (present or late, None when unknown) is what billing reads.
    """
    stays = _stays(clock, spans)
    lo, until = clock["start"], clock["count_until"]
    seconds = sum((min(b, until) - max(a, lo)).total_seconds() for a, b in stays)
    minutes = math.ceil(seconds / 60)
    late_minutes = _whole_minutes(stays[0][0] - lo) if stays else 0
    required = clock["required_minutes"]
    attended = bool(stays) and minutes >= required
    judged = "absent" if not attended else "late" if late_minutes > VERDICT_LATE_AFTER_MINUTES else "present"

    clock_unknown = unknown_in_room and not clock["follows_teacher"]
    held_back = clock_unknown or (unknown_in_room and judged != "present")
    return {"verdict": None if held_back else judged, "held_back": held_back,
            "attended": None if clock_unknown or (unknown_in_room and not attended) else attended,
            "minutes": minutes, "required": required, "late_minutes": late_minutes}


def student_flags(clock: dict, spans: list, mark: Optional[str], judged: dict) -> list:
    """Timing on the verdict's clock, and the marks that disagree with it about attending."""
    flags = []
    stays = _stays(clock, spans)
    if stays and judged["late_minutes"] > VERDICT_LATE_AFTER_MINUTES:
        flags.append({"code": "late", "minutes": judged["late_minutes"]})
    if stays and _whole_minutes(clock["end"] - stays[-1][1]) > STUDENT_LEFT_EARLY_AFTER_MINUTES:
        flags.append({"code": "left_early", "minutes": _whole_minutes(clock["end"] - stays[-1][1])})
    # Present versus late is not a disagreement — late counts as present — only attending is.
    if mark in ("present", "late") and judged["attended"] is False:
        if stays:
            flags.append({"code": "marked_present_too_short", "minutes": judged["minutes"],
                          "required": judged["required"]})
        else:
            flags.append({"code": "marked_present_not_joined"})
    if mark == "absent" and judged["attended"] is True:
        flags.append({"code": "marked_absent_was_in_room", "minutes": judged["minutes"]})
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


def _is_lesson_call(conference, start: datetime, end: datetime, now: datetime) -> bool:
    """A call that overlaps the lesson itself — not a room check before it."""
    return min(conference.ended_at or now, end) - max(conference.started_at or start, start) >= LESSON_CALL_MIN_OVERLAP


def _waiting(conferences: list, start: datetime, end: datetime, now: datetime) -> dict:
    """What a lesson that is not judged yet is waiting for — the pages say it instead of «Loading» (2026-09-15).

    The stage, first match wins: the lesson is still on; Google Meet still shows a call open; a call
    has ended and the LMS is saving who joined; no call of the lesson itself has come through from
    Google yet; or the lesson's call is saved and the record opens at ``ready_at``.
    """
    calls = sorted(conferences, key=lambda c: c.started_at or start)
    if now < end:
        stage = "lesson_running"
    elif any(c.ended_at is None for c in calls):
        stage = "call_open"
    elif any(c.synced_at is None for c in calls):
        stage = "collecting"
    elif not any(c.synced_at and _is_lesson_call(c, start, end, now) for c in calls):
        stage = "awaiting_google"
    else:
        stage = "settling"
    return {
        "stage": stage,
        "ended_at": utc_z(end),
        "ready_at": utc_z(end + COMPLETE_AFTER),
        "judge_at": utc_z(end + GIVE_UP_WAITING_AFTER),
        "calls": [{"started_at": utc_z(c.started_at) if c.started_at else None,
                   "ended_at": utc_z(c.ended_at) if c.ended_at else None,
                   "saved": c.synced_at is not None,
                   "lesson_call": _is_lesson_call(c, start, end, now)} for c in calls],
    }


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
        return {**unjudged, "state": "waiting", "waiting": _waiting(conferences, start, end, now)}
    if not any(c.synced_at for c in conferences):
        return {**unjudged, "state": "unavailable" if now > end + GOOGLE_KEEPS else "none"}
    lesson_call_saved = any(c.synced_at and _is_lesson_call(c, start, end, now) for c in conferences)
    if not lesson_call_saved and now < end + GIVE_UP_WAITING_AFTER:
        # Only calls around the lesson so far, not the lesson's own.
        return {**unjudged, "state": "waiting", "waiting": _waiting(conferences, start, end, now)}

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
    clock = lesson_clock(start, end, spans_of.get(event.teacher_id, []) if event.teacher_id else [])

    def person(uid: int, role: str, flags: list, **extra) -> dict:
        user = batch.users.get(uid)
        return {"user_id": uid, "name": user.name if user else f"User {uid}", "role": role,
                "mark": marks.get(uid), "accounts": accounts_of.get(uid, []),
                **_presence(spans_of.get(uid, []), start, end), "flags": flags, **extra}

    def student(uid: int) -> dict:
        spans = spans_of.get(uid, [])
        judged = verdict(clock, spans, unknown_in_room=unconfirmed)
        return person(uid, "student", student_flags(clock, spans, marks.get(uid), judged), verdict=judged)

    teacher = None
    if event.teacher_id:
        teacher = person(event.teacher_id, "teacher",
                         teacher_flags(start, end, spans_of.get(event.teacher_id, []), unconfirmed_in_room=unconfirmed))
    students = sorted((student(uid) for uid in student_ids), key=lambda p: p["name"].lower())
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
        "rules": verdict_rules(),
        "clock": {"start": utc_z(clock["start"]), "end": utc_z(clock["end"]),
                  "count_until": utc_z(clock["count_until"]), "held_minutes": clock["held_minutes"],
                  "required_minutes": clock["required_minutes"], "follows_teacher": clock["follows_teacher"]},
        "verdict_summary": verdict_summary(students),
    }


def verdict_rules() -> dict:
    """The rules as numbers, for the pages' legend — one source of truth."""
    return {"late_after_minutes": VERDICT_LATE_AFTER_MINUTES, "present_share": VERDICT_PRESENT_SHARE}


def verdict_summary(students: list) -> dict:
    """A lesson's verdicts counted: by verdict, how many wait on an account, how many students have
    no mark yet (and how many of those a verdict could fill), and — among marked students Meet can
    judge — how many marks agree with it about attending."""
    summary = {"present": 0, "late": 0, "absent": 0, "held_back": 0,
               "unmarked": 0, "applicable": 0, "compared": 0, "agree": 0}
    for s in students:
        judged, mark = s["verdict"], s.get("mark")
        if judged["held_back"]:
            summary["held_back"] += 1
        else:
            summary[judged["verdict"]] += 1
        if mark is None:
            summary["unmarked"] += 1
            summary["applicable"] += judged["verdict"] is not None
        elif mark in ("present", "late", "absent") and judged["attended"] is not None:
            summary["compared"] += 1
            summary["agree"] += (mark != "absent") == judged["attended"]
    return summary


def compact_verdicts(record: dict) -> list:
    """Each student's verdict in a few fields — what the list carries for the attendance journal."""
    return [{"user_id": s["user_id"], **{k: s["verdict"][k] for k in
                                         ("verdict", "held_back", "minutes", "required", "late_minutes")}}
            for s in record.get("students") or []]


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
        return {"name": p["name"], "mark": p.get("mark"), **presence(p),
                "flags": [flag(f) for f in p.get("flags", []) if f["code"] not in VERDICT_ONLY_CODES]}

    if record.get("state") != "ready":
        return {
            "state": record.get("state"),
            # Times and stage only — the calls carry no identities.
            "waiting": record.get("waiting"),
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
