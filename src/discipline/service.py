"""The register head teachers read: a half-month of lessons, judged, with decisions laid over.

Nothing is precomputed. A period is a few hundred lessons and the Meet record answers them in one
pass, so the page is always as current as the data behind it — a call Google hands over late
corrects an open period by itself. A closed period answers from its frozen totals instead, because
payroll has already been paid on them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, exists, or_, true
from sqlalchemy.orm import Session, aliased

from src.discipline.models import DisciplineDecision, DisciplinePeriod
from src.discipline.rules import RULE_START, Finding, Period, judge_lesson
from src.schemas.models import (CourseGroupAccess, CourseHeadTeacher, Event, EventGroup, Group,
                                UserInDB)
from src.services import meet_presence
from src.services.operational_groups import event_has_operational_group_clause

ALMATY = timedelta(hours=5)

#: What a day's cell says, worst news first — the grid paints this.
_STATE_ORDER = ("miss", "late", "ended_early", "unmeasurable", "clear", "none")

REASONS = (
    ("substitute", "Урок провёл другой преподаватель"),
    ("moved", "Урок перенесён или отменён"),
    ("technical", "Технические проблемы"),
    ("other", "Другое"),
)


class PeriodNotReady(Exception):
    """Raised when a period cannot be closed yet — a miss still has no price."""


class PeriodClosed(Exception):
    """Raised when something would change a period that payroll has already been paid on."""


def almaty_day(moment: datetime) -> date:
    return (moment + ALMATY).date()


def _now() -> datetime:
    """Wrapped so a test can hold the clock still without freezing the whole process."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now() -> datetime:
    return _now()


def visible_lessons_clause(user: UserInDB):
    """SQL: the lessons this person may read and decide on.

    Scope follows the **lesson**, not who owns the group. Asking «which groups does this teacher
    own» loses every substitute: on production one teacher taught six NUET lessons of somebody
    else's group, and no head teacher could see her — only admins (2026-09-18). A head teacher
    sees the lessons of the courses they manage (`course_head_teachers` → `course_group_access` →
    `event_groups`), whoever stood in front of the class, plus any lesson they taught themselves.
    """
    role = getattr(user, "role", None)
    if role == "admin":
        return true()
    if role != "head_teacher":
        return Event.teacher_id == user.id

    link = aliased(EventGroup)
    managed = (exists().where(and_(
        link.event_id == Event.id,
        CourseGroupAccess.group_id == link.group_id,
        CourseGroupAccess.is_active.is_(True),
        CourseHeadTeacher.course_id == CourseGroupAccess.course_id,
        CourseHeadTeacher.head_teacher_id == user.id,
    )).correlate(Event))
    return or_(managed, Event.teacher_id == user.id)


def may_touch_lesson(db: Session, user: UserInDB, event_id: int) -> bool:
    """Whether this person may decide on that lesson — the same rule, asked about one lesson."""
    if getattr(user, "role", None) == "admin":
        return True
    return db.query(Event.id).filter(Event.id == event_id, visible_lessons_clause(user)).first() is not None


def teachers_of(db: Session, user: UserInDB) -> Optional[list[int]]:
    """The teachers this person may decide on when there is no lesson to point at (a manual entry)."""
    role = getattr(user, "role", None)
    if role == "admin":
        return None
    if role != "head_teacher":
        return [user.id]
    rows = (db.query(Event.teacher_id)
            .filter(visible_lessons_clause(user), Event.teacher_id.isnot(None))
            .distinct().all())
    return sorted({teacher_id for (teacher_id,) in rows} | {user.id})


def closed_periods(db: Session) -> list[DisciplinePeriod]:
    return db.query(DisciplinePeriod).filter(DisciplinePeriod.closed_at.isnot(None)).all()


def _utc_bounds(period: Period) -> tuple[datetime, datetime]:
    """The period's Almaty days as the naive UTC range the events table stores."""
    first = max(period.start, RULE_START)
    starts_at = datetime.combine(first, datetime.min.time()) - ALMATY
    ends_at = datetime.combine(period.end + timedelta(days=1), datetime.min.time()) - ALMATY
    return starts_at, ends_at


def lessons_in(db: Session, period: Period, now: datetime, scope=None) -> list[Event]:
    """Lessons of the period that have already finished, never before the rule started.

    The open period runs to the end of the month, so most of it has not happened yet. A lesson
    still to come is not one the LMS «could not watch» — it is not a lesson yet, and counting it
    as unmeasurable painted the rest of the month grey (573 of 674 on 18.09).
    """
    starts_at, ends_at = _utc_bounds(period)
    return (db.query(Event)
            .filter(Event.event_type == "class", Event.is_active.is_(True),
                    Event.teacher_id.isnot(None),
                    Event.start_datetime >= starts_at, Event.start_datetime < ends_at,
                    Event.end_datetime <= now,
                    event_has_operational_group_clause(),
                    scope if scope is not None else true())
            .order_by(Event.start_datetime).all())


def _parse(moment: Optional[str]) -> Optional[datetime]:
    """The Meet record's `...Z` timestamps, back to the naive UTC the rest of the code uses."""
    if not moment:
        return None
    return datetime.fromisoformat(moment.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def _timings(db: Session, events: list[Event], now: datetime) -> dict[int, tuple]:
    """(state, first join, last leave, students at the end, students) per lesson, from Meet."""
    out: dict[int, tuple] = {}
    for record in meet_presence.records(db, events, now):
        teacher = record.get("teacher") or {}
        students = record.get("students") or []
        ended_at = _parse(record.get("end"))
        at_end = sum(1 for s in students
                     if (_parse(s.get("last_leave")) or datetime.min) >= (ended_at or datetime.max) - timedelta(minutes=5))
        out[record["event_id"]] = (record.get("state"), _parse(teacher.get("first_join")),
                                   _parse(teacher.get("last_leave")), at_end, len(students))
    return out


def _programs(db: Session, events: list[Event]) -> dict[int, str]:
    """Each lesson's programme, from the first group it belongs to — the sheet's tabs."""
    if not events:
        return {}
    rows = (db.query(EventGroup.event_id, Group.program_type, Group.name)
            .join(Group, Group.id == EventGroup.group_id)
            .filter(EventGroup.event_id.in_([e.id for e in events]))
            .all())
    out: dict[int, tuple[str, str]] = {}
    for event_id, program, name in rows:
        out.setdefault(event_id, ((program or "—").upper(), name))
    return out


def _decisions(db: Session, event_ids: Iterable[int]) -> dict[tuple, DisciplineDecision]:
    ids = list(event_ids)
    if not ids:
        return {}
    rows = db.query(DisciplineDecision).filter(DisciplineDecision.event_id.in_(ids)).all()
    return {(row.event_id, row.teacher_id, row.kind): row for row in rows}


def _empty_cell() -> dict:
    return {"late_minutes": 0, "early_minutes": 0, "misses": 0, "fine": 0, "unpriced": 0,
            "lessons": 0, "measured": 0, "unmeasurable": 0, "decided": 0, "state": "none"}


def _worst(states: set[str]) -> str:
    for state in _STATE_ORDER:
        if state in states:
            return state
    return "none"


def _stored_period(db: Session, period: Period) -> Optional[DisciplinePeriod]:
    return db.query(DisciplinePeriod).filter(DisciplinePeriod.period_key == period.key).first()


def judged_lessons(db: Session, period: Period, now: datetime, scope=None) -> list[dict]:
    """Every lesson of the period with its findings — the one place the rule meets the data."""
    events = lessons_in(db, period, now, scope)
    timings = _timings(db, events, now)
    programs = _programs(db, events)
    decisions = _decisions(db, (e.id for e in events))
    out = []
    for event in events:
        state, first_join, last_leave, at_end, students = timings.get(event.id, ("no_room", None, None, 0, 0))
        measurable = state == "ready"
        findings = judge_lesson(start=event.start_datetime, end=event.end_datetime,
                                first_join=first_join, last_leave=last_leave, measurable=measurable)
        program, group_name = programs.get(event.id, ("—", ""))
        out.append({
            "event": event, "teacher_id": event.teacher_id, "day": almaty_day(event.start_datetime),
            "program": program, "group": group_name, "measurable": measurable, "state": state,
            "first_join": first_join, "last_leave": last_leave,
            "students": students, "students_at_end": at_end,
            "findings": [{**f.__dict__,
                          "decision": decisions.get((event.id, event.teacher_id, f.kind))}
                         for f in findings],
        })
    return out


def _amount_of(finding: dict) -> tuple[int, bool]:
    """What a finding is worth now, and whether it still waits for a person to price it."""
    decision = finding.get("decision")
    if decision is not None:
        return int(decision.amount or 0), False
    if finding["fine"] is None:  # a miss: only a head teacher can price it
        return 0, True
    return int(finding["fine"]), False


def register(db: Session, period: Period, *, viewer: Optional[UserInDB] = None,
             teacher_ids: Optional[list[int]] = None, program: Optional[str] = None,
             now: Optional[datetime] = None) -> dict:
    """The grid: teachers down, days across, totals at the edges.

    `viewer` decides which lessons are in it — a head teacher's courses, a teacher's own lessons,
    everything for an admin. `teacher_ids` narrows further, for a caller that wants one row.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    scope = visible_lessons_clause(viewer) if viewer is not None else None
    stored = _stored_period(db, period)
    days = [period.start + timedelta(days=offset) for offset in range((period.end - period.start).days + 1)]

    rows: dict[int, dict] = {}
    # Every programme the period holds for this reader, whatever they are filtering by: the page's
    # tabs come from here, and a list that shrank to the chosen programme left no way back.
    programs: set[str] = set()
    for lesson in judged_lessons(db, period, now, scope):
        if teacher_ids is not None and lesson["teacher_id"] not in teacher_ids:
            continue
        programs.add(lesson["program"])
        if program and lesson["program"] != program.upper():
            continue
        row = rows.setdefault(lesson["teacher_id"], {
            "teacher_id": lesson["teacher_id"], "name": "", "program": lesson["program"],
            "days": {}, "totals": {"late_minutes": 0, "early_minutes": 0, "misses": 0,
                                   "fine": 0, "unpriced": 0, "lessons": 0, "unmeasurable": 0}})
        cell = row["days"].setdefault(lesson["day"].isoformat(), _empty_cell())
        states = {cell["state"]} - {"none"}

        cell["lessons"] += 1
        row["totals"]["lessons"] += 1
        if not lesson["measurable"]:
            cell["unmeasurable"] += 1
            row["totals"]["unmeasurable"] += 1
            states.add("unmeasurable")
        else:
            cell["measured"] += 1
            states.add("clear")
        for finding in lesson["findings"]:
            amount, unpriced = _amount_of(finding)
            states.add(finding["kind"])
            cell["fine"] += amount
            row["totals"]["fine"] += amount
            cell["unpriced"] += int(unpriced)
            row["totals"]["unpriced"] += int(unpriced)
            cell["decided"] += int(finding.get("decision") is not None)
            if finding["kind"] == "late":
                cell["late_minutes"] += finding["minutes"]
                row["totals"]["late_minutes"] += finding["minutes"]
            elif finding["kind"] == "ended_early":
                cell["early_minutes"] += finding["minutes"]
                row["totals"]["early_minutes"] += finding["minutes"]
            else:
                cell["misses"] += 1
                row["totals"]["misses"] += 1
        cell["state"] = _worst(states)

    for teacher in db.query(UserInDB).filter(UserInDB.id.in_(list(rows) or [0])).all():
        rows[teacher.id]["name"] = teacher.name

    teachers = sorted(rows.values(), key=lambda r: (r["program"], r["name"].lower()))
    totals = {key: sum(row["totals"][key] for row in teachers)
              for key in ("late_minutes", "early_minutes", "misses", "fine", "unpriced", "lessons", "unmeasurable")}
    closed = bool(stored and stored.closed_at)
    if closed and stored.totals:
        totals = {**totals, **stored.totals}
        for row in teachers:
            frozen = (stored.totals.get("by_teacher") or {}).get(str(row["teacher_id"]))
            if frozen:
                row["totals"] = frozen

    return {
        "period": {"key": period.key, "label": period.label, "start": period.start.isoformat(),
                   "end": period.end.isoformat(), "closed": closed,
                   "closed_at": stored.closed_at.isoformat() + "Z" if closed else None},
        "days": [day.isoformat() for day in days],
        "programs": sorted(programs),
        "teachers": teachers,
        "totals": totals,
        "reasons": [{"code": code, "label": label} for code, label in REASONS],
    }


def day_detail(db: Session, teacher_id: int, day: date, *, viewer: Optional[UserInDB] = None,
               now: Optional[datetime] = None) -> dict:
    """One teacher, one day: every lesson with its timings, findings and decision."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    from src.discipline.rules import period_containing
    period = period_containing(day)
    if period is None:
        return {"day": day.isoformat(), "teacher_id": teacher_id, "lessons": []}

    scope = visible_lessons_clause(viewer) if viewer is not None else None
    lessons = []
    for lesson in judged_lessons(db, period, now, scope):
        if lesson["teacher_id"] != teacher_id or lesson["day"] != day:
            continue
        event = lesson["event"]
        lessons.append({
            "event_id": event.id,
            "title": event.title,
            "group": lesson["group"] or lesson["program"],
            "program": lesson["program"],
            "starts_at": event.start_datetime.isoformat() + "Z",
            "ends_at": event.end_datetime.isoformat() + "Z",
            "measurable": lesson["measurable"],
            "state": lesson["state"],
            "first_join": lesson["first_join"].isoformat() + "Z" if lesson["first_join"] else None,
            "last_leave": lesson["last_leave"].isoformat() + "Z" if lesson["last_leave"] else None,
            "students": lesson["students"],
            "students_at_end": lesson["students_at_end"],
            "findings": [{
                "kind": finding["kind"], "minutes": finding["minutes"], "fine": finding["fine"],
                "made_up": finding["made_up"],
                "decision": _decision_json(finding.get("decision"), db),
            } for finding in lesson["findings"]],
        })
    return {"day": day.isoformat(), "teacher_id": teacher_id, "lessons": lessons,
            "reasons": [{"code": code, "label": label} for code, label in REASONS]}


def _decision_json(decision: Optional[DisciplineDecision], db: Session) -> Optional[dict]:
    if decision is None:
        return None
    author = db.query(UserInDB).filter(UserInDB.id == decision.decided_by).first()
    return {"amount": decision.amount, "reason_code": decision.reason_code, "note": decision.note,
            "by": author.name if author else None,
            "at": decision.decided_at.isoformat() + "Z" if decision.decided_at else None}


def apply_decision(db: Session, *, actor: UserInDB, event_id: Optional[int], teacher_id: int,
                   day: date, kind: str, amount: int, reason_code: Optional[str] = None,
                   note: Optional[str] = None, minutes: Optional[int] = None,
                   proposed_amount: Optional[int] = None) -> DisciplineDecision:
    """Record what a head teacher settled. One decision per lesson and kind; the last one wins."""
    from src.discipline.rules import period_containing
    period = period_containing(day)
    if period is None:
        raise ValueError(f"the rule starts on {RULE_START.strftime('%d.%m.%Y')}")
    stored = _stored_period(db, period)
    if stored and stored.closed_at:
        raise PeriodClosed(period.key)

    decision = (db.query(DisciplineDecision)
                .filter(DisciplineDecision.event_id == event_id,
                        DisciplineDecision.teacher_id == teacher_id,
                        DisciplineDecision.kind == kind).first())
    if decision is None:
        decision = DisciplineDecision(event_id=event_id, teacher_id=teacher_id, kind=kind, day=day)
        db.add(decision)
    decision.day = day
    decision.minutes = minutes
    decision.proposed_amount = proposed_amount
    decision.amount = max(0, int(amount or 0))
    decision.reason_code = reason_code
    decision.note = note
    decision.decided_by = actor.id
    decision.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
    return decision


def close_period(db: Session, period: Period, actor: UserInDB, *, now: Optional[datetime] = None) -> DisciplinePeriod:
    """Freeze a period's totals for payroll. Refuses while a miss still has no price."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    current = register(db, period, now=now)   # closing freezes the whole period, not one reader's view
    if current["totals"]["unpriced"]:
        raise PeriodNotReady(f"{current['totals']['unpriced']} missed lessons still have no amount")

    stored = _stored_period(db, period)
    if stored is None:
        stored = DisciplinePeriod(period_key=period.key, starts_on=period.start, ends_on=period.end)
        db.add(stored)
    stored.closed_at = now
    stored.closed_by = actor.id
    stored.totals = {**current["totals"],
                     "by_teacher": {str(row["teacher_id"]): row["totals"] for row in current["teachers"]}}
    db.flush()
    return stored
