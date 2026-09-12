"""The Recordings library: every lesson recording the viewer may watch, newest first.

Scope comes from ``recording_access.watchable_event_clause`` — the same rule playback and the
calendar ask — so the library can never list a recording its own Watch button would refuse.
Students see finished recordings only; staff also see ones still processing or failed, which
is how a teacher learns a lesson did not come through.

Never cached. Preview links carry a media token minted for the caller, exactly like the
playback URL, so a shared cache entry would hand one viewer's token to everyone.

Paging is keyset on (lesson start, event id), descending: stable while recordings arrive,
unlike OFFSET, which would repeat or skip a card whenever a new lesson lands at the top.

Days are Almaty days: a lesson at 23:30 in Almaty belongs to that date, not to the UTC one.
"""
import base64
import binascii
import re
from collections import Counter
from datetime import date as Date, datetime, time, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import Event, EventGroup, Group, LessonRecording, UserInDB
from src.services.media_tokens import signed_hls_url
from src.services.recording_access import public_status, watchable_event_clause

router = APIRouter()

PAGE_MAX = 48
PERIOD_DAYS = {"7d": 7, "30d": 30}
# Kazakhstan: one UTC+5 zone all year, no DST. Lessons are stored as naive UTC.
ALMATY = timedelta(hours=5)
MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _utc(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 with a Z, the convention EventSchema uses for the calendar."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _encode_cursor(start: datetime, event_id: int) -> str:
    raw = f"{start.isoformat()}|{event_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        start, event_id = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
        return datetime.fromisoformat(start), int(event_id)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _in_group(group_id: int):
    link = aliased(EventGroup)
    return exists().where(and_(link.event_id == Event.id, link.group_id == group_id)).correlate(Event)


def _almaty_day_starts(first: Date, days: int) -> tuple:
    """The naive-UTC instants that open and close `days` Almaty days from `first`."""
    start = datetime.combine(first, time.min) - ALMATY
    return start, start + timedelta(days=days)


def _almaty_today() -> Date:
    return (datetime.now(timezone.utc).replace(tzinfo=None) + ALMATY).date()


def _matches(term: str):
    link, group = aliased(EventGroup), aliased(Group)
    pattern = _like(term)
    return or_(
        Event.title.ilike(pattern, escape="\\"),
        exists()
        .where(and_(link.event_id == Event.id, link.group_id == group.id,
                    group.name.ilike(pattern, escape="\\")))
        .correlate(Event),
    )


def _scope(db: Session, current_user):
    """Every recording this viewer may see — the one access rule both the list and the calendar read."""
    scoped = (
        db.query(LessonRecording, Event)
        .join(Event, Event.id == LessonRecording.event_id)
        .filter(watchable_event_clause(current_user))
    )
    if current_user.role == "student":
        # A student has nothing to do with a recording that is not watchable yet.
        scoped = scoped.filter(LessonRecording.status == "ready", LessonRecording.hls_url.isnot(None))
    return scoped


def _narrow(query, current_user, *, q, group_id, teacher_id, status):
    """The filter menus and the search box — everything but the time range."""
    if current_user.role == "student":
        status = None  # already only the finished ones
    if status == "ready":
        query = query.filter(LessonRecording.status == "ready", LessonRecording.hls_url.isnot(None))
    elif status:
        query = query.filter(LessonRecording.status == status)
    if group_id is not None:
        query = query.filter(_in_group(group_id))
    if teacher_id is not None:
        query = query.filter(Event.teacher_id == teacher_id)
    if q and q.strip():
        query = query.filter(_matches(q.strip()))
    return query


def _time_narrow(query, *, period, day):
    """Apply the library's date range once, so every recording view agrees.

    The folder browser is navigation metadata, not a second authority about which lessons
    exist. Keeping this beside ``_narrow`` prevents a completed group from appearing in one
    view and silently disappearing in the other.
    """
    if day is not None:
        start, end = _almaty_day_starts(day, 1)
        return query.filter(Event.start_datetime >= start, Event.start_datetime < end)
    if period in PERIOD_DAYS:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=PERIOD_DAYS[period])
        return query.filter(Event.start_datetime >= since)
    return query


@router.get("")
def list_recordings(
    limit: int = Query(24, ge=1, le=PAGE_MAX),
    cursor: Optional[str] = Query(None, max_length=200),
    q: Optional[str] = Query(None, max_length=100),
    group_id: Optional[int] = None,
    teacher_id: Optional[int] = None,
    period: Literal["7d", "30d", "all"] = "all",
    status: Optional[Literal["ready", "pending", "failed"]] = None,
    day: Optional[Date] = Query(None, alias="date", description="One Almaty day, YYYY-MM-DD; wins over period"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    scoped = _scope(db, current_user)
    filtered = _narrow(scoped, current_user, q=q, group_id=group_id, teacher_id=teacher_id, status=status)
    filtered = _time_narrow(filtered, period=period, day=day)

    page = filtered
    if cursor:
        start, last_id = _decode_cursor(cursor)
        page = page.filter(or_(Event.start_datetime < start,
                               and_(Event.start_datetime == start, Event.id < last_id)))
    rows = page.order_by(Event.start_datetime.desc(), Event.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    event_ids = [event.id for _, event in rows]
    groups_by_event: dict = {}
    if event_ids:
        for event_id, gid, name in (
            db.query(EventGroup.event_id, Group.id, Group.name)
            .join(Group, Group.id == EventGroup.group_id)
            .filter(EventGroup.event_id.in_(event_ids))
            .order_by(Group.name)
            .all()
        ):
            groups_by_event.setdefault(event_id, []).append({"id": gid, "name": name})
    teacher_ids = {event.teacher_id for _, event in rows if event.teacher_id}
    teacher_names = dict(
        db.query(UserInDB.id, UserInDB.name).filter(UserInDB.id.in_(teacher_ids)).all()
    ) if teacher_ids else {}

    items = []
    for recording, event in rows:
        state = public_status(recording)
        items.append({
            "event_id": event.id,
            "title": event.title,
            "topic": event.topic,
            "start_datetime": _utc(event.start_datetime),
            "end_datetime": _utc(event.end_datetime),
            "groups": groups_by_event.get(event.id, []),
            "teacher": ({"id": event.teacher_id, "name": teacher_names.get(event.teacher_id)}
                        if event.teacher_id else None),
            "status": state,
            "duration_seconds": recording.duration_seconds,
            "poster_url": (signed_hls_url(recording.poster_url, current_user.id)
                           if state == "ready" and recording.poster_url else None),
            "ingested_at": _utc(recording.ingested_at),
        })

    last = rows[-1][1] if rows else None
    response = {
        "items": items,
        "next_cursor": _encode_cursor(last.start_datetime, last.id) if has_more and last else None,
    }
    if not cursor:
        response["total"] = filtered.count()
        response["facets"] = _facets(db, scoped)
    return response


def _group_state(*, is_active, is_over) -> str:
    """Lifecycle labels for recording history; neither stopped state removes a folder."""
    if is_over:
        return "finished"
    if is_active is False:
        return "archived"
    return "active"


@router.get("/folders")
def recording_folders(
    q: Optional[str] = Query(None, max_length=100),
    group_id: Optional[int] = None,
    teacher_id: Optional[int] = None,
    period: Literal["7d", "30d", "all"] = "all",
    status: Optional[Literal["ready", "pending", "failed"]] = None,
    day: Optional[Date] = Query(None, alias="date", description="One Almaty day, YYYY-MM-DD; wins over period"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """A compact, access-controlled Teacher → Group index for the folders view.

    It intentionally has no media URLs. Opening a group still asks the existing paged
    ``GET /recordings`` endpoint, which is the one place that creates a viewer-scoped preview
    token. The teacher is the instructor who actually taught the occurrence; the group's
    regular teacher accompanies any substituted recordings so the history stays auditable.
    """
    filtered = _time_narrow(
        _narrow(_scope(db, current_user), current_user,
                q=q, group_id=group_id, teacher_id=teacher_id, status=status),
        period=period, day=day,
    )
    taught_by = aliased(UserInDB)
    regular_teacher = aliased(UserInDB)
    rows = (
        filtered
        .join(EventGroup, EventGroup.event_id == Event.id)
        .join(Group, Group.id == EventGroup.group_id)
        .outerjoin(taught_by, taught_by.id == Event.teacher_id)
        .outerjoin(regular_teacher, regular_teacher.id == Group.teacher_id)
        .with_entities(
            Event.id,
            Event.teacher_id, taught_by.name,
            Group.id, Group.name, Group.is_active, Group.is_over,
            Group.teacher_id, regular_teacher.name,
        )
        .all()
    )

    # There is one row per recording/group association. Aggregate in Python rather than
    # relying on database-specific boolean SUM semantics; this keeps it just as portable as
    # the current library queries and correctly preserves multi-group lessons.
    teachers: dict = {}
    for (_event_id, taught_id, taught_name, gid, group_name, is_active, is_over,
         regular_id, regular_name) in rows:
        teacher_key = taught_id if taught_id is not None else "unassigned"
        teacher = teachers.setdefault(teacher_key, {
            "id": taught_id,
            "name": taught_name,
            "video_count": 0,
            "groups": {},
        })
        teacher["video_count"] += 1
        group = teacher["groups"].setdefault(gid, {
            "id": gid,
            "name": group_name,
            "state": _group_state(is_active=is_active, is_over=is_over),
            "video_count": 0,
            "substitution_count": 0,
            "regular_teacher": (
                {"id": regular_id, "name": regular_name} if regular_id is not None else None
            ),
        })
        group["video_count"] += 1
        if taught_id is not None and regular_id is not None and taught_id != regular_id:
            group["substitution_count"] += 1

    state_order = {"active": 0, "finished": 1, "archived": 2}
    result = []
    for teacher in teachers.values():
        groups = sorted(
            teacher["groups"].values(),
            key=lambda group: (state_order[group["state"]], group["name"].casefold(), group["id"]),
        )
        result.append({
            "id": teacher["id"],
            "name": teacher["name"],
            "video_count": teacher["video_count"],
            "group_count": len(groups),
            "groups": groups,
        })
    result.sort(key=lambda teacher: ((teacher["name"] or "").casefold(), teacher["id"] or -1))
    return {"teachers": result}


@router.get("/days")
def recording_days(
    month: Optional[str] = Query(None, max_length=7, description="YYYY-MM; default this Almaty month"),
    q: Optional[str] = Query(None, max_length=100),
    group_id: Optional[int] = None,
    teacher_id: Optional[int] = None,
    status: Optional[Literal["ready", "pending", "failed"]] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """How many recordings each Almaty day of a month holds, under the list's own filters — the
    Recordings calendar marks those days. Days without any are left out."""
    if month is None:
        first = _almaty_today().replace(day=1)
    else:
        found = MONTH.match(month)
        if not found:
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        first = Date(int(found.group(1)), int(found.group(2)), 1)
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    start, end = _almaty_day_starts(first, (following - first).days)

    starts = (
        _narrow(_scope(db, current_user), current_user, q=q, group_id=group_id,
                teacher_id=teacher_id, status=status)
        .filter(Event.start_datetime >= start, Event.start_datetime < end)
        .with_entities(Event.start_datetime)
        .all()
    )
    days = Counter((moment + ALMATY).date().isoformat() for (moment,) in starts)
    return {"month": first.strftime("%Y-%m"), "days": dict(sorted(days.items())), "total": sum(days.values())}


def _facets(db: Session, scoped) -> dict:
    """The groups and teachers that occur in the viewer's library, for its filter menus.

    Taken from the unfiltered scope so choosing one group does not empty the other menus.
    """
    ids = scoped.with_entities(Event.id).subquery()
    groups = (
        db.query(Group.id, Group.name, Group.is_active, Group.is_over)
        .join(EventGroup, EventGroup.group_id == Group.id)
        .filter(EventGroup.event_id.in_(select(ids.c.id)))
        .distinct()
        .order_by(Group.name)
        .all()
    )
    teacher_ids = scoped.with_entities(Event.teacher_id).filter(Event.teacher_id.isnot(None)).subquery()
    teachers = (
        db.query(UserInDB.id, UserInDB.name)
        .filter(UserInDB.id.in_(select(teacher_ids.c.teacher_id)))
        .order_by(UserInDB.name)
        .all()
    )
    return {
        "groups": [
            {"id": gid, "name": name, "is_active": is_active, "is_over": is_over}
            for gid, name, is_active, is_over in groups
        ],
        "teachers": [{"id": uid, "name": name} for uid, name in teachers],
    }
