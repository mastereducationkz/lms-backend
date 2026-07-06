from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from src.courses.models import Group
from src.events.models import Event, EventGroup


def _resolve_planned_lessons(group: Group, total_events: int) -> int:
    schedule_config = group.schedule_config or {}
    if isinstance(schedule_config, dict):
        lessons_count = schedule_config.get("lessons_count")
        if isinstance(lessons_count, int) and lessons_count > 0:
            return lessons_count
    return total_events


def get_groups_over_status_changes(db: Session, group_ids: Optional[Iterable[int]] = None) -> list[tuple[Group, bool]]:
    query = db.query(Group)
    if group_ids:
        query = query.filter(Group.id.in_(list(group_ids)))

    groups = query.all()
    if not groups:
        return []

    now = datetime.utcnow()

    # Batch-fetch class events for every group in one query instead of one
    # query per group — this function used to be an N+1 hotspot on any page
    # that scopes to "all groups" (e.g. head curator dashboards).
    event_rows = (
        db.query(EventGroup.group_id, Event.start_datetime)
        .join(Event, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id.in_([g.id for g in groups]),
            Event.event_type == "class",
            Event.is_active == True,
        )
        .all()
    )
    events_by_group: dict[int, list] = {}
    for group_id, start_datetime in event_rows:
        events_by_group.setdefault(group_id, []).append(start_datetime)

    changes: list[tuple[Group, bool]] = []

    for group in groups:
        event_datetimes = [dt for dt in events_by_group.get(group.id, []) if dt is not None]

        if not event_datetimes:
            should_be_over = False
        else:
            total_events = len(event_datetimes)
            planned_lessons = _resolve_planned_lessons(group, total_events)
            past_lessons = sum(1 for dt in event_datetimes if dt < now)
            has_future_lessons = any(dt >= now for dt in event_datetimes)
            should_be_over = planned_lessons > 0 and past_lessons >= planned_lessons and not has_future_lessons

        if group.is_over != should_be_over:
            changes.append((group, should_be_over))

    return changes


def sync_groups_over_status(db: Session, group_ids: Optional[Iterable[int]] = None) -> int:
    changes = get_groups_over_status_changes(db, group_ids)
    for group, should_be_over in changes:
        group.is_over = should_be_over

    if changes:
        db.commit()

    return len(changes)
