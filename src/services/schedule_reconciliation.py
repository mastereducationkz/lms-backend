"""
Schedule reconciliation: preserve attendance when regenerating group schedules.

Order-based matching:
- Pair the i-th upcoming existing event with the i-th upcoming desired slot.
- Move matched events in place (keep event.id -> attendance preserved) so a
  day/time change is a SHIFT, not a delete+recreate.
- Create events for extra future slots; deactivate extra future events.
- Never touch past events, and never (re)create past lessons.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

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
    from sqlalchemy import exists as sa_exists
    from src.events.models import Event, EventGroup
    from src.lesson_requests.models import LessonRequest

    now_utc = datetime.utcnow()
    approved_sub = sa_exists().where(
        (LessonRequest.event_id == Event.id)
        & (LessonRequest.request_type == "substitution")
        & (LessonRequest.status == "approved")
    )
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


def reconcile_group_schedule(
    db: Session,
    group_id: int,
    desired_slots: List[Tuple[datetime, int]],
    group_name: str,
    teacher_id: Optional[int],
    created_by: int,
) -> dict:
    """
    Reconcile desired lesson slots with existing class events for a group.
    - desired_slots: [(target_dt_utc, lesson_number), ...]
    - Returns counters: updated, created, deactivated, rebound
    """
    from src.events.models import Event, EventGroup

    now_utc = datetime.now(timezone.utc)

    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    existing_events = (
        db.query(Event)
        .join(EventGroup)
        .filter(
            EventGroup.group_id == group_id,
            Event.event_type == "class",
            Event.is_active == True,
        )
        .all()
    )

    # Past events are history — never moved or deactivated here.
    past_existing = [e for e in existing_events if _as_utc(e.start_datetime) < now_utc]
    future_existing = sorted(
        (e for e in existing_events if _as_utc(e.start_datetime) >= now_utc),
        key=lambda e: _as_utc(e.start_datetime),
    )
    # Only future slots are actionable (we never (re)create past lessons).
    future_desired = sorted(
        ((dt, ln) for dt, ln in desired_slots if _as_utc(dt) >= now_utc),
        key=lambda item: _as_utc(item[0]),
    )

    updated = 0
    created = 0
    deactivated = 0

    # Order-based matching: move the i-th upcoming event onto the i-th new slot.
    # Moving in place keeps event.id, so attendance/history stay attached and a
    # day/time change becomes a SHIFT (nothing is lost, count preserved).
    pair_count = min(len(future_existing), len(future_desired))
    for i in range(pair_count):
        event = future_existing[i]
        target_dt, _ln = future_desired[i]
        end_dt = target_dt + timedelta(minutes=60)
        if (
            _as_utc(event.start_datetime) != _as_utc(target_dt)
            or _as_utc(event.end_datetime) != _as_utc(end_dt)
        ):
            event.start_datetime = target_dt
            event.end_datetime = end_dt
            event.updated_at = now_utc
            updated += 1
        event.teacher_id = teacher_id

    # Extra new slots (schedule now has more future lessons) -> create.
    for target_dt, _ln in future_desired[pair_count:]:
        end_dt = target_dt + timedelta(minutes=60)
        new_event = Event(
            title=f"{group_name}: Lesson",
            description=f"Scheduled class for {group_name}",
            event_type="class",
            start_datetime=target_dt,
            end_datetime=end_dt,
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

    # Extra upcoming events (schedule now has fewer future lessons) -> deactivate.
    for event in future_existing[pair_count:]:
        event.is_active = False
        event.updated_at = now_utc
        deactivated += 1

    db.flush()

    # Sequential titles across all active class events (by date) — no gaps.
    active_sorted = sorted(
        db.query(Event)
        .join(EventGroup)
        .filter(
            EventGroup.group_id == group_id,
            Event.event_type == "class",
            Event.is_active == True,
        )
        .all(),
        key=lambda e: _as_utc(e.start_datetime),
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
    }
