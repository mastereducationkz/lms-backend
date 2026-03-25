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
    now = datetime.utcnow()
    changes: list[tuple[Group, bool]] = []

    for group in groups:
        class_events = (
            db.query(Event.start_datetime)
            .join(EventGroup, EventGroup.event_id == Event.id)
            .filter(
                EventGroup.group_id == group.id,
                Event.event_type == "class",
                Event.is_active == True,
            )
            .all()
        )

        if not class_events:
            should_be_over = False
        else:
            event_datetimes = [row[0] for row in class_events if row[0] is not None]
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
