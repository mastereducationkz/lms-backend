"""The Recordings library: every lesson recording the viewer may watch, newest first.

Scope comes from ``recording_access.watchable_event_clause`` — the same rule playback and the
calendar ask — so the library can never list a recording its own Watch button would refuse.
Students see finished recordings only; staff also see ones still processing or failed, which
is how a teacher learns a lesson did not come through.

Never cached. Preview links carry a media token minted for the caller, exactly like the
playback URL, so a shared cache entry would hand one viewer's token to everyone.

Paging is keyset on (lesson start, event id), descending: stable while recordings arrive,
unlike OFFSET, which would repeat or skip a card whenever a new lesson lands at the top.
"""
import base64
import binascii
from datetime import datetime, timedelta, timezone
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


@router.get("")
def list_recordings(
    limit: int = Query(24, ge=1, le=PAGE_MAX),
    cursor: Optional[str] = Query(None, max_length=200),
    q: Optional[str] = Query(None, max_length=100),
    group_id: Optional[int] = None,
    teacher_id: Optional[int] = None,
    period: Literal["7d", "30d", "all"] = "all",
    status: Optional[Literal["ready", "pending", "failed"]] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    scoped = (
        db.query(LessonRecording, Event)
        .join(Event, Event.id == LessonRecording.event_id)
        .filter(watchable_event_clause(current_user))
    )
    if current_user.role == "student":
        # A student has nothing to do with a recording that is not watchable yet.
        scoped = scoped.filter(LessonRecording.status == "ready", LessonRecording.hls_url.isnot(None))
        status = None

    filtered = scoped
    if status == "ready":
        filtered = filtered.filter(LessonRecording.status == "ready", LessonRecording.hls_url.isnot(None))
    elif status:
        filtered = filtered.filter(LessonRecording.status == status)
    if period in PERIOD_DAYS:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=PERIOD_DAYS[period])
        filtered = filtered.filter(Event.start_datetime >= since)
    if group_id is not None:
        filtered = filtered.filter(_in_group(group_id))
    if teacher_id is not None:
        filtered = filtered.filter(Event.teacher_id == teacher_id)
    if q and q.strip():
        filtered = filtered.filter(_matches(q.strip()))

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


def _facets(db: Session, scoped) -> dict:
    """The groups and teachers that occur in the viewer's library, for its filter menus.

    Taken from the unfiltered scope so choosing one group does not empty the other menus.
    """
    ids = scoped.with_entities(Event.id).subquery()
    groups = (
        db.query(Group.id, Group.name)
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
        "groups": [{"id": gid, "name": name} for gid, name in groups],
        "teachers": [{"id": uid, "name": name} for uid, name in teachers],
    }
