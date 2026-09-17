"""
Schedule reconciliation: update only future lessons, preserve past ones and their attendance.

The rules themselves — how many lessons a counted course still needs and which existing lesson
goes where — live in :mod:`src.services.schedule_plan` (the LMS mirror of the CRM's
``src/groups/schedule_plan.py``). This module reads the group's lessons, hands them to those
rules and writes the outcome, as the CRM's ``src/groups/schedule_reconciliation.py`` does, so a
schedule saved in either system produces the same lessons. A save
(:func:`apply_group_schedule`) and its preview (:func:`preview_group_schedule`) share those
reads and rules, so they cannot disagree.

- A lesson already on a desired slot keeps it; the rest move in date order (moving in place
  keeps ``event.id``, so attendance and history stay attached).
- Extra future slots are created; extra future lessons are deactivated.
- Past lessons are never touched, and past lessons are never (re)created.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.services.schedule_plan import (
    KZ_TZ,
    LessonSpan,
    _parse_start_date,
    future_schedule_slots,
    is_counted_schedule,
    plan_schedule_changes,
    weekly_slot_minutes,
)

logger = logging.getLogger(__name__)


def sync_future_lesson_teachers(db: Session, group_id: int, new_teacher_id: Optional[int]) -> int:
    """Re-point FUTURE active class lessons of a group at its (new) teacher.

    Group teacher reassignment historically updated only ``groups.teacher_id``,
    leaving every already-generated event at the old teacher — the new teacher
    then saw "Substituted by <old>" on all lessons and lost the group in their
    salary breakdown. Past events are salary history and are never touched;
    events with an APPROVED substitution request keep their substitute.

    Returns the number of events updated.
    """
    from src.events.models import Event, EventGroup
    from src.services.lesson_teacher import approved_substitution_exists_clause

    now_utc = datetime.utcnow()
    approved_sub = approved_substitution_exists_clause()
    updated = (
        db.query(Event)
        .filter(
            Event.id.in_(
                db.query(EventGroup.event_id).filter(EventGroup.group_id == group_id)
            ),
            Event.event_type == "class",
            Event.is_active == True,
            Event.start_datetime >= now_utc,
            ~approved_sub,
        )
        .update({Event.teacher_id: new_teacher_id}, synchronize_session=False)
    )
    if updated:
        logger.info(
            "sync_future_lesson_teachers group_id=%s new_teacher_id=%s updated=%s",
            group_id, new_teacher_id, updated,
        )
    return updated


#: A slot is ``(datetime, lesson number, duration minutes)``. Older callers (the bulk group
#: import) pass the first two only; those groups all ran 60-minute lessons, which is what the
#: default preserves.
DEFAULT_SLOT_MINUTES = 60


def _slot(item) -> Tuple[datetime, int, int]:
    if len(item) >= 3:
        return (item[0], item[1], int(item[2] or DEFAULT_SLOT_MINUTES))
    return (item[0], item[1], DEFAULT_SLOT_MINUTES)


def _normalize_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.replace(second=0, microsecond=0)


def _now_utc(now: Optional[datetime]) -> datetime:
    """``now`` as an aware UTC instant; naive means UTC (how events are stored)."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def reconcile_group_schedule(
    db: Session,
    group_id: int,
    desired_slots: List[Tuple],
    group_name: str,
    teacher_id: Optional[int],
    created_by: int,
    *,
    previous_minutes: Optional[dict[tuple[int, str], int]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Match desired future slots to existing class events.
    Past events are never modified.

    ``desired_slots`` holds ``(target_dt_utc, lesson_number)`` or
    ``(target_dt_utc, lesson_number, minutes)``; the two-item form means 60 minutes.

    Pairing follows :func:`plan_schedule_changes`: a lesson already on a slot keeps it, the rest
    move in date order. ``previous_minutes`` is the weekly pattern's lengths before this save
    (see :func:`weekly_slot_minutes`); with it, a lesson that stays put keeps a length set by
    hand unless its own day's length changed. ``None`` lets every slot decide, as it always did.
    ``now`` splits past from future (default: the wall clock).
    """
    from src.events.models import Event, EventGroup
    from src.services.lesson_teacher import (
        assign_lesson_teacher_preserving_overrides,
        protected_event_teachers,
    )

    now_utc = _now_utc(now)
    # Row timestamps are when the write happened, not the (possibly pinned) planning instant.
    now_naive = datetime.utcnow()

    # Occurrence-level overrides — an approved substitution pins a teacher to one lesson and
    # outranks the group's regular teacher for it. Read once, before anything moves, so the
    # outcome does not depend on the order events happen to be paired in.
    protected = protected_event_teachers(db, group_id)

    existing_events = (
        db.query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id == group_id,
            Event.event_type == "class",
            Event.is_active == True,
        )
        .all()
    )

    # Past events are history — never moved or deactivated here.
    past_existing = [e for e in existing_events if _normalize_dt(e.start_datetime) < now_utc]
    future_existing = sorted(
        (e for e in existing_events if _normalize_dt(e.start_datetime) >= now_utc),
        key=lambda e: _normalize_dt(e.start_datetime),
    )
    # Only future slots are actionable (we never (re)create past lessons).
    future_desired = sorted(
        (_slot(item) for item in desired_slots if _normalize_dt(item[0]) >= now_utc),
        key=lambda item: _normalize_dt(item[0]),
    )

    updated = 0
    created = 0
    deactivated = 0

    changes = plan_schedule_changes(
        [LessonSpan(e.id, e.start_datetime, e.end_datetime) for e in future_existing],
        [(dt, minutes) for dt, _ln, minutes in future_desired],
        previous_minutes,
    )
    by_id = {e.id: e for e in future_existing}
    for change in changes:
        if change.kind == "create":
            # No description: «Scheduled class for {group}» repeated the title and the group
            # line, and went stale on every rename — 14 804 of 16 189 were wrong when the owner
            # had them removed (2026-09-15).
            new_event = Event(
                title=f"{group_name}: Lesson",
                description=None,
                event_type="class",
                start_datetime=change.start.replace(tzinfo=None),
                end_datetime=change.end.replace(tzinfo=None),
                location="Online",
                is_online=True,
                created_by=created_by,
                teacher_id=teacher_id,
                is_active=True,
                is_recurring=False,
                max_participants=50,
            )
            db.add(new_event)
            db.flush()
            db.add(EventGroup(event_id=new_event.id, group_id=group_id))
            created += 1
            continue
        event = by_id[change.event_id]
        if change.kind == "deactivate":
            event.is_active = False
            event.updated_at = now_naive
            deactivated += 1
            continue
        if change.kind in ("move", "resize"):
            # Moving in place keeps event.id, so attendance, the Meet room and history stay
            # attached and a day/time change becomes a SHIFT (nothing is lost, count preserved).
            event.start_datetime = change.start.replace(tzinfo=None)
            event.end_datetime = change.end.replace(tzinfo=None)
            event.updated_at = now_naive
            updated += 1
        # NOT `event.teacher_id = teacher_id`. That line handed a substituted lesson back to
        # the group's regular teacher on the next schedule edit, undoing an approved
        # substitution without anybody asking for it.
        assign_lesson_teacher_preserving_overrides(event, teacher_id, protected)

    db.flush()

    # Sequential titles across all active class events (by date) — no gaps.
    active_sorted = sorted(
        db.query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id == group_id,
            Event.event_type == "class",
            Event.is_active == True,
        )
        .all(),
        key=lambda e: _normalize_dt(e.start_datetime),
    )
    for idx, event in enumerate(active_sorted, start=1):
        title = f"{group_name}: Lesson {idx}"
        if event.title != title:
            event.title = title
    db.flush()

    logger.info(
        "schedule_reconciliation group_id=%s updated=%s created=%s deactivated=%s past=%s",
        group_id,
        updated,
        created,
        deactivated,
        len(past_existing),
    )
    return {
        "updated": updated,
        "created": created,
        "deactivated": deactivated,
        "rebound": 0,
        "past_preserved": len(past_existing),
        "changes": changes,
    }


# ------------------------------------------------------------------ save and preview


@dataclass
class ScheduleState:
    """What a schedule save starts from: the lessons already begun and the ones to come."""

    #: Active class lessons that started before ``now`` — they count towards «Кол-во уроков».
    started_count: int
    started_minutes: int
    #: Active class lessons starting at or after ``now``, in date order.
    future_events: list
    #: Start instants (naive UTC, as stored) of future lessons removed by an approved
    #: cancellation. A save must not put a lesson back on them.
    cancelled_instants: set


def approved_cancelled_event_ids(db: Session, event_ids: Iterable[int]) -> set[int]:
    """The events among ``event_ids`` an approved cancel request names.

    Only an approved decision counts: a pending or rejected request, or a lesson switched off
    by hand in the CRM («Удалить урок»), is not a cancellation and the next save plans the
    course again without it.
    """
    from src.lesson_requests.models import LessonRequest
    from src.services.lesson_teacher import APPROVED_STATUS

    ids = [int(i) for i in event_ids]
    if not ids:
        return set()
    rows = (
        db.query(LessonRequest.event_id)
        .filter(
            LessonRequest.event_id.in_(ids),
            LessonRequest.request_type == "cancel",
            LessonRequest.status == APPROVED_STATUS,
        )
        .all()
    )
    return {int(r[0]) for r in rows if r[0] is not None}


def _lesson_minutes(event) -> int:
    if event.start_datetime is None or event.end_datetime is None:
        return 0
    return max(0, int((event.end_datetime - event.start_datetime).total_seconds() // 60))


def load_schedule_state(db: Session, group_id: int, now: datetime) -> ScheduleState:
    from src.events.models import Event, EventGroup

    now_utc = _now_utc(now)
    rows = [
        e
        for e in (
            db.query(Event)
            .join(EventGroup, EventGroup.event_id == Event.id)
            .filter(EventGroup.group_id == group_id, Event.event_type == "class")
            .all()
        )
        if e.start_datetime is not None
    ]
    started = [e for e in rows if e.is_active and _normalize_dt(e.start_datetime) < now_utc]
    future = sorted(
        (e for e in rows if e.is_active and _normalize_dt(e.start_datetime) >= now_utc),
        key=lambda e: (_normalize_dt(e.start_datetime), e.id),
    )
    inactive_future = [
        e for e in rows if not e.is_active and _normalize_dt(e.start_datetime) >= now_utc
    ]
    cancelled_ids = approved_cancelled_event_ids(db, [e.id for e in inactive_future])
    return ScheduleState(
        started_count=len(started),
        started_minutes=sum(_lesson_minutes(e) for e in started),
        future_events=future,
        cancelled_instants={e.start_datetime for e in inactive_future if e.id in cancelled_ids},
    )


def desired_schedule_slots(
    config: Any,
    state: ScheduleState,
    now: datetime,
    fallback_start: Optional[date],
) -> list[tuple[datetime, int]]:
    """The future lessons ``config`` asks for, given what the group has already had."""
    return future_schedule_slots(
        config,
        started=state.started_count,
        now=now,
        skip_instants=state.cancelled_instants,
        fallback_start=fallback_start,
    )


def apply_group_schedule(
    db: Session,
    group_id: int,
    config: Any,
    *,
    previous_config: Any,
    group_name: str,
    teacher_id: Optional[int],
    created_by: int,
    fallback_start: Optional[date],
    now: Optional[datetime] = None,
) -> dict:
    """Write ``config``'s lessons for the group — what ``POST /curator/schedule/generate`` does.

    The same save as the CRM's ``PATCH /groups/{id}/schedule``: the course is counted from the
    lessons already begun, approved cancellations stay cancelled, lessons on a kept slot stay put.
    """
    now_utc = _now_utc(now)
    state = load_schedule_state(db, group_id, now_utc)
    slots = desired_schedule_slots(config, state, now_utc, fallback_start)
    return reconcile_group_schedule(
        db,
        group_id,
        [(dt, i, minutes) for i, (dt, minutes) in enumerate(slots, start=1)],
        group_name,
        teacher_id,
        created_by,
        previous_minutes=weekly_slot_minutes(previous_config),
        now=now_utc,
    )


def duplicate_lesson_rows(db: Session, group_id: int) -> int:
    """Extra active class lessons sharing a start minute, past and future alike.

    Rows at duplicated instants minus the number of such instants. The CRM's save merges these
    before it counts; the LMS save does not, so each extra row is counted — a taught duplicate
    as a lesson taught, a future one as a lesson to pair — and the preview has to say so.
    """
    from src.events.models import Event, EventGroup

    per_instant: dict[datetime, int] = {}
    rows = (
        db.query(Event.start_datetime)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id == group_id,
            Event.event_type == "class",
            Event.is_active == True,  # noqa: E712
        )
        .all()
    )
    for (start,) in rows:
        if start is None:
            continue
        key = _now_utc(start).replace(second=0, microsecond=0)
        per_instant[key] = per_instant.get(key, 0) + 1
    return sum(count - 1 for count in per_instant.values() if count > 1)


def _iso_utc(moment: datetime) -> str:
    """An instant for the wire, always with its offset: naive is UTC, as events are stored.

    An offset-less string is read by a browser as the viewer's local time, five hours off in
    Almaty — the preview must never hand one out.
    """
    return _now_utc(moment).isoformat()


def _span_minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def preview_group_schedule(
    db: Session,
    group_id: int,
    config: Any,
    *,
    previous_config: Any,
    fallback_start: Optional[date],
    now: Optional[datetime] = None,
) -> dict:
    """What :func:`apply_group_schedule` would do with ``config`` — computed, never written.

    Same ``now`` split, lessons-taught count, approved-cancellation skipping and pairing as the
    save (the same three calls); twin of the CRM's ``preview_group_schedule``, with the same keys.
    """
    from src.services.group_bot_digest import plural

    now_utc = _now_utc(now)
    state = load_schedule_state(db, group_id, now_utc)
    slots = desired_schedule_slots(config, state, now_utc, fallback_start)
    changes = plan_schedule_changes(
        [LessonSpan(e.id, e.start_datetime, e.end_datetime) for e in state.future_events],
        slots,
        weekly_slot_minutes(previous_config),
    )
    kept = [c for c in changes if c.kind != "deactivate"]
    planned_minutes = sum(_span_minutes(c.start, c.end) for c in kept)
    warnings: list[str] = []
    if is_counted_schedule(config) and int(config["lessons_count"]) <= state.started_count:
        warnings.append(
            f"Кол-во уроков ({int(config['lessons_count'])}) не больше, чем уже прошло "
            f"({state.started_count}) — новых уроков не будет"
        )
    # A start date ahead of today reads like «a new cycle from then», but the lessons already
    # taught still count towards «Кол-во уроков» — a reused group would plan fewer than meant.
    start = _parse_start_date(config, None)
    if state.started_count > 0 and start and start > now_utc.astimezone(KZ_TZ).date():
        lessons_word = plural(state.started_count, "урок", "урока", "уроков")
        warnings.append(
            f"Дата начала позже сегодняшней, но у группы уже прошло {state.started_count} "
            f"{lessons_word} — они засчитаны в «Кол-во уроков»; для нового цикла создайте "
            "новую группу или увеличьте количество."
        )
    # Unlike the CRM's, the LMS save does not merge duplicates first: the numbers above already
    # count them exactly as the save will, and the warning says the save will not tidy them up.
    extra_rows = duplicate_lesson_rows(db, group_id)
    if extra_rows:
        warnings.append(
            f"Есть дубли уроков ({extra_rows}) — сохранение их не объединит, итог посчитан с ними."
        )
    return {
        "started_lessons": state.started_count,
        "started_minutes": state.started_minutes,
        "planned_lessons": len(kept),
        "planned_minutes": planned_minutes,
        "total_lessons": state.started_count + len(kept),
        "total_minutes": state.started_minutes + planned_minutes,
        "first_start": _iso_utc(kept[0].start) if kept else None,
        "last_end": _iso_utc(kept[-1].end) if kept else None,
        "lessons": [
            {
                "start": _iso_utc(c.start),
                "end": _iso_utc(c.end),
                "minutes": _span_minutes(c.start, c.end),
                "event_id": c.event_id,
                "change": c.kind,
                "previous_start": _iso_utc(c.previous_start) if c.previous_start else None,
                "previous_end": _iso_utc(c.previous_end) if c.previous_end else None,
            }
            for c in kept
        ],
        "deactivated": [
            {"event_id": c.event_id, "start": _iso_utc(c.start), "end": _iso_utc(c.end)}
            for c in changes
            if c.kind == "deactivate"
        ],
        "warnings": warnings,
    }
