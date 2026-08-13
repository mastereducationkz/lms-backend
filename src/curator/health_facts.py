"""Raw academic facts for the CRM's student-health engine.

Ownership, stated once: the **LMS owns the facts** — who attended, who submitted, who opened
a lesson — and the **CRM owns the judgement** — what counts as at-risk, whose problem it is,
when it is overdue. So this module computes counts and dates and returns them; it contains no
thresholds and no notion of severity. Moving a threshold must be a CRM settings change, not
an LMS deploy.

Everything is batched over the whole cohort. A per-student query here would be an N+1 across
a service boundary, which is the same mistake as an N+1 in a loop but a hundred times more
expensive.

Two counting rules matter more than the rest, because getting them wrong produces confident
nonsense:

**Unmarked is missing data, not absence.** A lesson nobody marked tells you nothing about the
student. It is excluded from both the numerator and the denominator, and reported separately
so a school that has stopped marking sees a data-quality problem rather than a wave of green.

**Denominators respect boundaries.** Lessons before the student joined the group, cancelled
lessons, and lessons inside a freeze period never count. A student who joined in March is not
responsible for February. A freeze is *scoped to a group*: freezing SAT drops the SAT lessons
and leaves every IELTS denominator exactly as it was, because the student is still attending.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.curator.freeze_mirror import GROUP_WIDE
from src.schemas.models import (
    Assignment,
    AssignmentExtension,
    AssignmentSubmission,
    Attendance,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    StepProgress,
    UserInDB,
)

#: Mirrors src/attendance/status.py in the CRM. `late` counts as attended — they were there.
PRESENT_STATUSES = frozenset({"present", "presented", "1", "yes", "late"})
ABSENT_STATUSES = frozenset({"absent", "0", "no", "missed"})
#: Deliberately excluded from both: the student was taken off this lesson's roster, so it is
#: neither a presence nor an absence for them.
REMOVED_STATUSES = frozenset({"removed", "excluded"})
MARKED_STATUSES = PRESENT_STATUSES | ABSENT_STATUSES

#: How far back the attendance window may reach. Bounded so one call cannot scan years.
MAX_WINDOW_DAYS = 120

#: Parsed shape: student → group → spans. Group :data:`GROUP_WIDE` covers every group.
ScopedSpans = dict[int, dict[int, list[tuple[Optional[date], Optional[date]]]]]


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _normalize(status: Optional[str]) -> str:
    return (status or "").strip().lower()


def health_facts(
    db: Session,
    student_ids: Iterable[int],
    *,
    window_days: int = 30,
    activity_days: int = 7,
    homework_window_days: int = 7,
    progress_days: int = 7,
    frozen_windows: Optional[dict[int, Any]] = None,
) -> dict[str, Any]:
    """Per student, per group, the counts the CRM's rules read.

    ``frozen_windows`` maps student id to the (start, end) ISO spans the CRM considers frozen,
    in either of two shapes: ``{student_id: [[start, end], ...]}`` (student-wide, every group)
    or ``{student_id: {group_id: [[start, end], ...]}}`` (one entry per frozen enrollment).
    Lessons inside a span that covers their group are dropped from every denominator. The CRM
    supplies them because the CRM owns the freeze lifecycle; the LMS neither stores nor infers
    it here.
    """
    ids = sorted({int(s) for s in student_ids})[:5000]
    if not ids:
        return {"students": {}}

    window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    today = date.today()
    now = datetime.utcnow()
    window_start = now - timedelta(days=window_days)

    out: dict[str, dict[str, Any]] = {
        str(sid): {
            "lms_student_id": sid,
            "last_activity_date": None,
            "groups": {},
            "missed_homework_recent": 0,
            "missed_homework_details": [],
            "last_progress_at": None,
            "has_homework_data": False,
            "has_progress_data": False,
            "has_frozen_scope": False,
            "frozen_group_ids": [],
        }
        for sid in ids
    }

    users = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(ids)).all()}
    for sid in ids:
        user = users.get(sid)
        if user is not None and user.last_activity_date:
            out[str(sid)]["last_activity_date"] = user.last_activity_date.isoformat()

    # Parsed before anything else reads a membership, because the fully frozen student is the
    # one who has no memberships left at all and still has to be distinguishable from a
    # student who simply lost their group — see :func:`_live_frozen_groups`.
    frozen = _parse_frozen(frozen_windows)
    for sid in ids:
        live = _live_frozen_groups(frozen.get(sid), today)
        if live:
            out[str(sid)]["has_frozen_scope"] = True
            out[str(sid)]["frozen_group_ids"] = live

    # --- membership --------------------------------------------------------------------
    memberships: dict[int, dict[int, Optional[date]]] = {sid: {} for sid in ids}
    group_meta: dict[int, dict[str, Any]] = {}
    for sid, gid, gname, program, curator_id, joined, active, over in (
        db.query(
            GroupStudent.student_id,
            Group.id,
            Group.name,
            Group.program_type,
            Group.curator_id,
            GroupStudent.created_at,
            Group.is_active,
            Group.is_over,
        )
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(GroupStudent.student_id.in_(ids))
        .all()
    ):
        if not bool(active) or bool(over):
            continue
        memberships[int(sid)][int(gid)] = _as_date(joined)
        group_meta[int(gid)] = {
            "group_id": int(gid),
            "name": (gname or "").strip() or f"Группа #{gid}",
            "product": (program or "").strip() or None,
            "curator_id": curator_id,
        }

    active_group_ids = sorted(group_meta)
    if not active_group_ids:
        _finish_homework_and_progress(
            db, ids, out, homework_window_days, progress_days, now
        )
        return {"students": out}

    # --- lessons -----------------------------------------------------------------------
    # One query for every lesson of every relevant group in the window, plus the attendance
    # rows for those lessons. Two queries total, regardless of cohort size.
    lesson_rows = (
        db.query(Event.id, Event.start_datetime, Event.end_datetime, Event.is_active, EventGroup.group_id)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id.in_(active_group_ids),
            Event.event_type == "class",
            Event.start_datetime >= window_start,
            Event.start_datetime <= now + timedelta(days=365),
        )
        .all()
    )
    lesson_ids = [int(r[0]) for r in lesson_rows]

    attendance: dict[tuple[int, int], str] = {}
    if lesson_ids:
        for event_id, user_id, status in (
            db.query(Attendance.event_id, Attendance.user_id, Attendance.status)
            .filter(Attendance.event_id.in_(lesson_ids), Attendance.user_id.in_(ids))
            .all()
        ):
            attendance[(int(event_id), int(user_id))] = _normalize(status)

    lessons_by_group: dict[int, list[tuple[int, datetime, bool]]] = {}
    for event_id, start, end, is_active, group_id in lesson_rows:
        lessons_by_group.setdefault(int(group_id), []).append(
            (int(event_id), start, bool(is_active))
        )
    for bucket in lessons_by_group.values():
        bucket.sort(key=lambda item: item[1])

    for sid in ids:
        entry = out[str(sid)]
        for group_id, joined in memberships[sid].items():
            lessons = lessons_by_group.get(group_id, [])
            past_marked: list[tuple[datetime, str]] = []
            future_count = 0
            unmarked_past = 0
            for event_id, start, is_active in lessons:
                if not is_active:
                    # Cancelled: never a denominator, never an absence.
                    continue
                start_date = _as_date(start)
                if start > now:
                    future_count += 1
                    continue
                if joined and start_date and start_date < joined:
                    # Before they joined. Not their lesson.
                    continue
                if _in_frozen_window(frozen.get(sid), group_id, start_date):
                    continue
                status = attendance.get((event_id, sid))
                if status in REMOVED_STATUSES:
                    continue
                if status is None or status not in MARKED_STATUSES:
                    unmarked_past += 1
                    continue
                past_marked.append((start, status))

            past_marked.sort(key=lambda item: item[0])
            attended = sum(1 for _, s in past_marked if s in PRESENT_STATUSES)
            total = len(past_marked)

            # Consecutive absences counted backwards from the most recent marked lesson.
            # Unmarked lessons were already skipped, so a gap in marking cannot fake a streak.
            streak = 0
            for _, status in reversed(past_marked):
                if status in ABSENT_STATUSES:
                    streak += 1
                else:
                    break

            meta = group_meta[group_id]
            entry["groups"][str(group_id)] = {
                **meta,
                "joined_at": joined.isoformat() if joined else None,
                "marked_lessons": total,
                "attended_lessons": attended,
                "absent_lessons": total - attended,
                "unmarked_past_lessons": unmarked_past,
                "attendance_rate": round(attended * 100.0 / total, 1) if total else None,
                "consecutive_absences": streak,
                "future_lessons": future_count,
                "last_lesson_at": past_marked[-1][0].isoformat() if past_marked else None,
                "window_days": window_days,
            }

    _finish_homework_and_progress(db, ids, out, homework_window_days, progress_days, now)
    return {"students": out}


def _parse_frozen(frozen_windows: Optional[dict[int, Any]]) -> ScopedSpans:
    """Both wire shapes, one internal shape.

    The flat legacy shape — a bare list of spans per student — means "every group", which is
    what it has always meant and the only reading that keeps an older CRM build correct while
    the two services roll out independently.
    """
    parsed: ScopedSpans = {}
    for sid, value in (frozen_windows or {}).items():
        per_group = value if isinstance(value, dict) else {GROUP_WIDE: value}
        by_group: dict[int, list[tuple[Optional[date], Optional[date]]]] = {}
        for gid, windows in (per_group or {}).items():
            spans: list[tuple[Optional[date], Optional[date]]] = []
            for start, end in windows or []:
                try:
                    spans.append(
                        (
                            date.fromisoformat(start) if start else None,
                            date.fromisoformat(end) if end else None,
                        )
                    )
                except (TypeError, ValueError):
                    continue
            if spans:
                by_group[int(gid)] = spans
        if by_group:
            parsed[int(sid)] = by_group
    return parsed


def _spans_cover(
    spans: Optional[list[tuple[Optional[date], Optional[date]]]], day: Optional[date]
) -> bool:
    if not spans or day is None:
        return False
    for start, end in spans:
        if start and day < start:
            continue
        if end and day > end:
            continue
        return True
    return False


def _in_frozen_window(
    scopes: Optional[dict[int, list[tuple[Optional[date], Optional[date]]]]],
    group_id: int,
    day: Optional[date],
) -> bool:
    """Does a freeze covering *this group* cover this day?

    A student-wide span covers every group; a scoped one covers only its own. This is the line
    that stops a SAT freeze from erasing an IELTS lesson from the denominator.
    """
    if not scopes:
        return False
    return _spans_cover(scopes.get(GROUP_WIDE), day) or _spans_cover(
        scopes.get(int(group_id)), day
    )


def _live_frozen_groups(
    scopes: Optional[dict[int, list[tuple[Optional[date], Optional[date]]]]],
    today: date,
) -> list[int]:
    """The scopes whose freeze covers today.

    Freezing now deletes the student's ``group_students`` row, so a fully frozen student comes
    back from this endpoint with no groups at all — which is indistinguishable, by shape
    alone, from a student who has simply lost their group. That is exactly the CRM's
    ``active_without_group`` signal, and without this flag it would fire on every student the
    school deliberately froze: the feature would announce itself as a wave of false cases.

    The rule stays in the CRM, which owns every judgement here. What the LMS owes it is the
    fact that makes the distinction possible, which is this list.
    """
    if not scopes:
        return []
    return sorted(gid for gid, spans in scopes.items() if _spans_cover(spans, today))


def _finish_homework_and_progress(
    db: Session,
    ids: list[int],
    out: dict[str, dict[str, Any]],
    homework_window_days: int,
    progress_days: int,
    now: datetime,
) -> None:
    """Missed required homework in the window, and the last time anything moved.

    A deadline extension is a deliberate act by a teacher; counting an extended homework as
    missed would produce a case the moment the teacher was helpful, which is the fastest way
    to teach curators to ignore the homework signal.
    """
    since = now - timedelta(days=max(1, int(homework_window_days)))

    assignments = (
        db.query(Assignment.id, Assignment.due_date)
        .filter(Assignment.due_date.isnot(None), Assignment.due_date >= since, Assignment.due_date <= now)
        .all()
    )
    assignment_ids = [int(a[0]) for a in assignments]
    due_by_id = {int(a[0]): a[1] for a in assignments}

    if assignment_ids:
        submitted = {
            (int(aid), int(uid))
            for aid, uid in db.query(
                AssignmentSubmission.assignment_id, AssignmentSubmission.user_id
            )
            .filter(
                AssignmentSubmission.assignment_id.in_(assignment_ids),
                AssignmentSubmission.user_id.in_(ids),
            )
            .all()
        }
        # `student_id`, not `user_id`. `AssignmentSubmission` above really does call its
        # column `user_id`, and this query was written to match its neighbour rather than the
        # model — so every call raised AttributeError before touching the database. The whole
        # endpoint 500'd, the CRM saw 502, and the health engine concluded that nobody had a
        # group: 1480 of 1493 students came out "critical", which blocked activation.
        extensions: dict[tuple[int, int], datetime] = {
            (int(aid), int(uid)): deadline
            for aid, uid, deadline in db.query(
                AssignmentExtension.assignment_id,
                AssignmentExtension.student_id,
                AssignmentExtension.extended_deadline,
            )
            .filter(
                AssignmentExtension.assignment_id.in_(assignment_ids),
                AssignmentExtension.student_id.in_(ids),
            )
            .all()
        }
        # Which students were even expected to do each assignment: membership in a group the
        # assignment was issued to. Without this every student "misses" every assignment in
        # the school.
        expected = _expected_students(db, assignment_ids, ids)

        for sid in ids:
            entry = out[str(sid)]
            missed: list[dict[str, Any]] = []
            for aid in assignment_ids:
                if sid not in expected.get(aid, set()):
                    continue
                entry["has_homework_data"] = True
                if (aid, sid) in submitted:
                    continue
                deadline = extensions.get((aid, sid)) or due_by_id.get(aid)
                if deadline and deadline > now:
                    continue
                missed.append({"assignment_id": aid, "due_at": deadline.isoformat() if deadline else None})
            entry["missed_homework_recent"] = len(missed)
            entry["missed_homework_details"] = missed[:10]

    progress_since = now - timedelta(days=max(1, int(progress_days)) * 4)
    for user_id, last_at in (
        db.query(StepProgress.user_id, func.max(StepProgress.completed_at))
        .filter(
            StepProgress.user_id.in_(ids),
            StepProgress.completed_at.isnot(None),
            StepProgress.completed_at >= progress_since,
        )
        .group_by(StepProgress.user_id)
        .all()
    ):
        entry = out[str(int(user_id))]
        entry["has_progress_data"] = True
        entry["last_progress_at"] = last_at.isoformat() if last_at else None


def _expected_students(
    db: Session, assignment_ids: list[int], student_ids: list[int]
) -> dict[int, set[int]]:
    """Students an assignment was actually issued to, via its groups."""
    from src.schemas.models import GroupAssignment

    rows = (
        db.query(GroupAssignment.assignment_id, GroupStudent.student_id)
        .join(GroupStudent, GroupStudent.group_id == GroupAssignment.group_id)
        .filter(
            GroupAssignment.assignment_id.in_(assignment_ids),
            GroupStudent.student_id.in_(student_ids),
        )
        .all()
    )
    out: dict[int, set[int]] = {}
    for aid, sid in rows:
        out.setdefault(int(aid), set()).add(int(sid))
    return out
