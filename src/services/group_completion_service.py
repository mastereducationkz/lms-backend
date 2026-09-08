"""Decide when a group is finished — and hold it open for a grace period after that.

Two rules live here, and the CRM mirrors them byte-for-byte in
``crm-master/backend/src/groups/completion.py``. **The two must agree**: they write the same
``groups.is_over`` column, so a disagreement makes the flag flip back and forth between the
systems. Constants are named identically on both sides on purpose.

1. A lesson is behind us only once it has **ended**. Reading ``start_datetime`` marked a
   group finished at the instant its final lesson began — teachers lost it off their list
   while the class was still in the room and attendance had yet to be taken.
2. Even a genuinely finished group stays open until the first Wednesday 23:59:59
   Asia/Almaty strictly after its last lesson ends. Inside that window ``is_over`` is
   ``False`` for everyone — curators, teachers, admins — so the group behaves exactly as it
   does today and nothing has to be configured per role.
"""
from datetime import datetime, time, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy.orm import Session

from src.courses.models import Group
from src.events.models import Event, EventGroup

#: Asia/Almaty is a fixed UTC+5 with no DST, and events are stored as naive UTC.
ALMATY_UTC_OFFSET = timedelta(hours=5)
#: ``datetime.weekday()``: Monday=0 … Wednesday=2. Move this to move the cutoff day.
GROUP_CLOSE_WEEKDAY = 2  # Wednesday
#: Local Almaty time of day the grace period expires at.
GROUP_CLOSE_TIME = time(23, 59, 59)
#: ``Event.end_datetime`` is ``nullable=False``, but legacy rows are not worth a 500.
DEFAULT_LESSON_DURATION = timedelta(minutes=60)

#: One lesson as ``(start_datetime, end_datetime)``, both naive UTC, either may be ``None``.
LessonBounds = tuple[Optional[datetime], Optional[datetime]]


def planned_lessons_count(schedule_config: Any, total_events: int) -> int:
    """Lessons the group is *supposed* to have: the schedule's count, else what exists."""
    if isinstance(schedule_config, dict):
        lessons_count = schedule_config.get("lessons_count")
        if isinstance(lessons_count, int) and lessons_count > 0:
            return lessons_count
    return total_events


def lesson_end(start: Optional[datetime], end: Optional[datetime]) -> Optional[datetime]:
    """When the lesson is over, falling back to ``start + 60 min`` for a null end."""
    if end is not None:
        return end
    if start is None:
        return None
    return start + DEFAULT_LESSON_DURATION


def group_close_deadline(last_lesson_end: datetime) -> datetime:
    """First Wednesday 23:59:59 Asia/Almaty **strictly after** ``last_lesson_end``, naive UTC.

    Documented assumption: the deadline is the first qualifying instant strictly after the
    last lesson ends, *not* "the following week". A course whose final lesson ends on a
    Wednesday at 10:00 therefore closes that **same** Wednesday evening at 23:59:59, leaving
    under fourteen hours of grace; only a lesson ending at or after that instant pushes the
    deadline to the next Wednesday. The owner's sentence ("leave it until the first Wednesday
    23:59") leaves this open, and this is the reading it is written down as.

    Almaty is a fixed UTC+5 with no DST, so the conversion is a plain offset both ways.
    """
    local = last_lesson_end + ALMATY_UTC_OFFSET
    days_ahead = (GROUP_CLOSE_WEEKDAY - local.weekday()) % 7
    candidate = datetime.combine(local.date() + timedelta(days=days_ahead), GROUP_CLOSE_TIME)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate - ALMATY_UTC_OFFSET


def compute_close_deadline(
    schedule_config: Any,
    lesson_bounds: Sequence[LessonBounds],
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """When this group will close, or ``None`` while it is still running.

    ``lesson_bounds`` must be the ``(start, end)`` datetimes of the group's ACTIVE class
    events, in naive UTC — the same set and the same clock the CRM uses, so both sides agree.

    "Still running" means it has no lessons, has taught fewer than it planned, or has a
    lesson that has not ended yet. A group inside its grace window is **not** running: it has
    a deadline here and ``is_over`` still ``False``, which is exactly what the window is.
    """
    moment = now or datetime.utcnow()
    ends = [lesson_end(start, end) for start, end in lesson_bounds]
    ends = [dt for dt in ends if dt is not None]
    if not ends:
        return None
    planned = planned_lessons_count(schedule_config, len(ends))
    past = sum(1 for dt in ends if dt <= moment)
    has_future = any(dt > moment for dt in ends)
    if not (planned > 0 and past >= planned and not has_future):
        return None
    return group_close_deadline(max(ends))


def compute_is_over(
    schedule_config: Any,
    lesson_bounds: Sequence[LessonBounds],
    now: Optional[datetime] = None,
) -> bool:
    """``True`` once every planned lesson has ended **and** the grace period has expired."""
    moment = now or datetime.utcnow()
    deadline = compute_close_deadline(schedule_config, lesson_bounds, moment)
    return deadline is not None and moment >= deadline


def _lesson_bounds_by_group(
    db: Session, group_ids: Sequence[int]
) -> dict[int, list[LessonBounds]]:
    """Active class events per group, as ``(start, end)`` pairs — one query, not N+1.

    This used to be an N+1 hotspot on any page that scopes to "all groups" (e.g. head
    curator dashboards).
    """
    if not group_ids:
        return {}
    rows = (
        db.query(EventGroup.group_id, Event.start_datetime, Event.end_datetime)
        .join(Event, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id.in_(list(group_ids)),
            Event.event_type == "class",
            Event.is_active == True,  # noqa: E712
        )
        .all()
    )
    bounds: dict[int, list[LessonBounds]] = {}
    for group_id, start_datetime, end_datetime in rows:
        bounds.setdefault(group_id, []).append((start_datetime, end_datetime))
    return bounds


def _load_groups(db: Session, group_ids: Optional[Iterable[int]]) -> list[Group]:
    query = db.query(Group)
    if group_ids:
        query = query.filter(Group.id.in_(list(group_ids)))
    return query.all()


def get_groups_over_status_changes(
    db: Session, group_ids: Optional[Iterable[int]] = None
) -> list[tuple[Group, bool]]:
    """Groups whose stored ``is_over`` disagrees with the rule, paired with the right value."""
    groups = _load_groups(db, group_ids)
    if not groups:
        return []

    now = datetime.utcnow()
    bounds_by_group = _lesson_bounds_by_group(db, [g.id for g in groups])

    changes: list[tuple[Group, bool]] = []
    for group in groups:
        should_be_over = compute_is_over(
            group.schedule_config, bounds_by_group.get(group.id, []), now
        )
        if bool(group.is_over) != should_be_over:
            changes.append((group, should_be_over))
    return changes


def get_groups_close_deadlines(
    db: Session, group_ids: Optional[Iterable[int]] = None
) -> dict[int, Optional[datetime]]:
    """``{group_id: close_deadline_utc | None}`` — ``None`` while the group is not finished.

    A non-null value with ``now`` before it is a group inside its grace window: still open
    to everyone, with a date the UI can show ("Закроется в среду 23:59").
    """
    groups = _load_groups(db, group_ids)
    if not groups:
        return {}

    now = datetime.utcnow()
    bounds_by_group = _lesson_bounds_by_group(db, [g.id for g in groups])
    return {
        group.id: compute_close_deadline(
            group.schedule_config, bounds_by_group.get(group.id, []), now
        )
        for group in groups
    }


def sync_groups_over_status(
    db: Session,
    group_ids: Optional[Iterable[int]] = None,
    *,
    commit: bool = True,
) -> int:
    """Recompute ``is_over`` for the given groups (all groups when ``None``).

    ``commit=False`` leaves the flip pending in the caller's transaction — for callers that
    are mid-way through an atomic change (approving a cancel deactivates a lesson, may
    append another, and must not have the group's status committed in between).
    """
    changes = get_groups_over_status_changes(db, group_ids)
    for group, should_be_over in changes:
        group.is_over = should_be_over

    if changes:
        if commit:
            db.commit()
        else:
            db.flush()

    return len(changes)
