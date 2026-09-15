"""What a subscribed calendar shows, read from the LMS — the one source for both the ICS feeds
and the Google Calendar sync, so the two can never disagree.

A group's calendar: its active class lessons and weekly tests from a week ago to two months
ahead, and the deadlines of its active, visible homework as all-day entries on their Almaty date.
Links point into the LMS (login required) — never at the raw Meet room, so a calendar link that
travels further than the group does not hand out lesson rooms.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, or_

from src.schemas.models import Assignment, Event, EventGroup, Group, GroupAssignment, GroupStudent
from src.services.calendar_ics import CalendarItem
from src.services.operational_groups import event_has_operational_group_clause
from src.services.recording_watch_links import lms_url

WINDOW_PAST = timedelta(days=7)
WINDOW_AHEAD = timedelta(days=60)
ALMATY_OFFSET = timedelta(hours=5)
STAFF_ROLES = frozenset({"teacher", "head_teacher", "curator", "head_curator", "admin"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_meet(url: Optional[str]) -> bool:
    return bool(url) and "meet.google.com" in url


def lesson_link(event_id: int) -> str:
    """The lesson inside the LMS calendar, where «Войти» picks the right account."""
    return lms_url(f"/calendar?event={event_id}")


def lesson_item(event, group_name: Optional[str]) -> CalendarItem:
    link = lesson_link(event.id)
    lines = [f"Тема: {event.topic}"] if event.topic else []
    lines.append(f"Войти на урок: {link}")
    return CalendarItem(
        key=f"lesson-{event.id}",
        summary=f"{group_name}: урок" if group_name else (event.title or "Урок"),
        description="\n".join(lines),
        start=event.start_datetime, end=event.end_datetime, url=link,
        updated=getattr(event, "updated_at", None),
    )


def weekly_item(event) -> CalendarItem:
    # A weekly test links to its platform set; a Meet link is never copied out.
    link = event.meeting_url if event.meeting_url and not _is_meet(event.meeting_url) else lesson_link(event.id)
    return CalendarItem(
        key=f"weekly-{event.id}", summary=_clean(event.title) or "Weekly mock",
        description=f"Открыть: {link}", start=event.start_datetime, end=event.end_datetime,
        url=link, updated=getattr(event, "updated_at", None),
    )


def _clean(title: Optional[str]) -> str:
    """Titles are typed by staff: « Maps » must not become «Дедлайн:  Maps  до 17:00»."""
    return " ".join((title or "").split())


def deadline_item(task) -> CalendarItem:
    local = task.due_date + ALMATY_OFFSET
    link = lms_url(f"/homework/{task.id}")
    return CalendarItem(
        key=f"deadline-{task.id}", summary=f"📝 Дедлайн: {_clean(task.title)} до {local:%H:%M}",
        description=f"Сдать в LMS: {link}", day=local.date(), url=link,
        updated=getattr(task, "updated_at", None),
    )


def _events(db, group_ids: Iterable[int], event_type: str, now: datetime, teacher_id: Optional[int] = None):
    query = (db.query(Event, EventGroup.group_id)
             .join(EventGroup, EventGroup.event_id == Event.id)
             .filter(Event.is_active.is_(True), Event.event_type == event_type,
                     Event.start_datetime >= now - WINDOW_PAST, Event.start_datetime < now + WINDOW_AHEAD,
                     event_has_operational_group_clause()))
    if teacher_id is not None:
        query = query.filter(Event.teacher_id == teacher_id)
    else:
        query = query.filter(EventGroup.group_id.in_(list(group_ids)))
    seen, rows = set(), []
    for event, group_id in query.order_by(Event.start_datetime, EventGroup.group_id).all():
        if event.id not in seen:
            seen.add(event.id)
            rows.append((event, group_id))
    return rows


def _deadlines(db, group_ids: list, now: datetime):
    if not group_ids:
        return []
    return (db.query(Assignment)
            .outerjoin(GroupAssignment, and_(GroupAssignment.assignment_id == Assignment.id,
                                             GroupAssignment.group_id.in_(group_ids),
                                             GroupAssignment.is_active.is_(True)))
            .filter(or_(Assignment.group_id.in_(group_ids), GroupAssignment.id.isnot(None)),
                    Assignment.is_active.is_(True), Assignment.is_hidden.is_(False),
                    Assignment.due_date.isnot(None),
                    Assignment.due_date >= now - WINDOW_PAST, Assignment.due_date < now + WINDOW_AHEAD)
            .distinct().all())


def group_items(db, group: Group, now: Optional[datetime] = None) -> list[CalendarItem]:
    now = now or _now()
    items = [lesson_item(event, group.name) for event, _ in _events(db, [group.id], "class", now)]
    items += [weekly_item(event) for event, _ in _events(db, [group.id], "weekly_test", now)]
    items += [deadline_item(task) for task in _deadlines(db, [group.id], now)]
    return items


def user_group_ids(db, user) -> list[int]:
    """The groups whose calendars a person may subscribe to: a student's own groups, the groups a
    teacher teaches, the groups a curator curates."""
    if user.role == "student":
        rows = (db.query(Group.id).join(GroupStudent, GroupStudent.group_id == Group.id)
                .filter(GroupStudent.student_id == user.id, Group.is_active.is_(True)).all())
    elif user.role in ("curator", "head_curator"):
        rows = db.query(Group.id).filter(Group.curator_id == user.id, Group.is_active.is_(True)).all()
    elif user.role in STAFF_ROLES:
        rows = db.query(Group.id).filter(Group.teacher_id == user.id, Group.is_active.is_(True)).all()
    else:
        rows = []
    return sorted({row[0] for row in rows})


def user_items(db, user, now: Optional[datetime] = None) -> list[CalendarItem]:
    """One person's own calendar: a student's groups; a teacher's lessons they actually teach
    (substitutions included, handed-off lessons excluded); a curator's groups."""
    now = now or _now()
    group_ids = user_group_ids(db, user)
    names = dict(db.query(Group.id, Group.name).filter(Group.id.in_(group_ids)).all()) if group_ids else {}
    if user.role in ("teacher", "head_teacher"):
        lessons = _events(db, [], "class", now, teacher_id=user.id)
        names.update(dict(db.query(Group.id, Group.name)
                          .filter(Group.id.in_({gid for _, gid in lessons})).all()) if lessons else {})
        return [lesson_item(event, names.get(gid)) for event, gid in lessons]
    if not group_ids:
        return []
    items = [lesson_item(event, names.get(gid)) for event, gid in _events(db, group_ids, "class", now)]
    items += [weekly_item(event) for event, _ in _events(db, group_ids, "weekly_test", now)]
    items += [deadline_item(task) for task in _deadlines(db, group_ids, now)]
    return items


def items_hash(items: Iterable[CalendarItem]) -> str:
    """Changes exactly when what the calendar should show changes."""
    rows = sorted(
        (i.key, i.summary, i.description, i.start.isoformat() if i.start else "",
         i.end.isoformat() if i.end else "", i.day.isoformat() if i.day else "", i.url or "")
        for i in items
    )
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
