"""
Schedule reconciliation: preserve attendance when regenerating group schedules.

Replaces mass deactivate + create with:
- Match existing future events to desired slots (exact time, then nearest within tolerance)
- Update matched events in place (keep event.id -> attendance preserved)
- Create new events for unmatched desired slots
- Deactivate only unmatched future events (never touch past)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from src.services.attendance_service import AttendanceService

logger = logging.getLogger(__name__)

TOLERANCE_MINUTES = 90


def _normalize_dt(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


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

    desired_by_time = {_normalize_dt(dt): (dt, ln) for dt, ln in desired_slots}

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

    future_events = [e for e in existing_events if _as_utc(e.start_datetime) >= now_utc]
    past_events = [e for e in existing_events if _as_utc(e.start_datetime) < now_utc]

    matched: List[Tuple[Event, datetime, int]] = []
    unmatched_desired: List[Tuple[datetime, int]] = []
    unmatched_existing: List[Event] = list(future_events)

    for event in future_events:
        sig = _normalize_dt(event.start_datetime)
        if sig in desired_by_time:
            dt, ln = desired_by_time.pop(sig)
            matched.append((event, dt, ln))
            unmatched_existing.remove(event)
            continue

        best_slot = None
        best_diff = timedelta(minutes=TOLERANCE_MINUTES + 1)
        for dsig, (dt, ln) in list(desired_by_time.items()):
            diff = abs((_as_utc(event.start_datetime) - _as_utc(dt)).total_seconds())
            if diff < best_diff.total_seconds():
                best_diff = timedelta(seconds=diff)
                best_slot = (dsig, dt, ln)

        if best_slot and best_diff <= timedelta(minutes=TOLERANCE_MINUTES):
            dsig, dt, ln = best_slot
            desired_by_time.pop(dsig)
            matched.append((event, dt, ln))
            unmatched_existing.remove(event)

    unmatched_desired = list(desired_by_time.values())

    updated = 0
    for event, target_dt, lesson_number in matched:
        end_dt = target_dt + timedelta(minutes=60)
        changed = (
            _as_utc(event.start_datetime) != _as_utc(target_dt)
            or _as_utc(event.end_datetime) != _as_utc(end_dt)
            or event.title != f"{group_name}: Lesson {lesson_number}"
        )
        if changed:
            event.start_datetime = target_dt
            event.end_datetime = end_dt
            event.title = f"{group_name}: Lesson {lesson_number}"
            event.teacher_id = teacher_id
            event.updated_at = now_utc
            updated += 1

    created = 0
    created_event_ids_by_dt: dict = {}
    for target_dt, lesson_number in unmatched_desired:
        end_dt = target_dt + timedelta(minutes=60)
        new_event = Event(
            title=f"{group_name}: Lesson {lesson_number}",
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
        created_event_ids_by_dt[_normalize_dt(target_dt)] = new_event.id
        created += 1

    rebound = 0
    for event in unmatched_existing:
        best_new_id = None
        best_diff_sec = TOLERANCE_MINUTES * 60 + 1
        for target_dt, _ in unmatched_desired:
            diff_sec = abs((_as_utc(event.start_datetime) - _as_utc(target_dt)).total_seconds())
            if diff_sec <= TOLERANCE_MINUTES * 60 and diff_sec < best_diff_sec:
                best_diff_sec = diff_sec
                sig = _normalize_dt(target_dt)
                if sig in created_event_ids_by_dt:
                    best_new_id = created_event_ids_by_dt[sig]
        if best_new_id:
            rebound += AttendanceService.rebind_event_attendance(
                db, event.id, best_new_id, flush=False
            )

    deactivated = 0
    for event in unmatched_existing:
        event.is_active = False
        event.updated_at = now_utc
        deactivated += 1

    db.flush()

    logger.info(
        "schedule_reconciliation group_id=%s updated=%s created=%s deactivated=%s rebound=%s",
        group_id,
        updated,
        created,
        deactivated,
        rebound,
    )
    return {
        "updated": updated,
        "created": created,
        "deactivated": deactivated,
        "rebound": rebound,
        "past_preserved": len(past_events),
    }
