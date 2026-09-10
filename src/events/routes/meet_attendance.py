"""Who was in a lesson's Meet room, from when to when — and the one-time "who is this account".

Read by admins, head curators and head teachers (every lesson), teachers (their lessons) and
curators (their groups' lessons); students never (owner, 2026-09-11). Anyone else gets 404,
not 403, the same as recordings. The meaning of the record lives in ``meet_presence``; this
file only checks access and shapes requests.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, exists
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import (
    Event,
    EventGroup,
    GoogleAccountLink,
    Group,
    MeetConference,
    MeetParticipant,
    UserInDB,
)
from src.services import meet_presence
from src.utils.utc_json import utc_z

router = APIRouter()

DEFAULT_DAYS = 30
MAX_DAYS = 62
MAX_LESSONS = 500


def _utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _visible_lesson(db, user, event_id: int) -> Event:
    event = (db.query(Event)
             .filter(Event.id == event_id, meet_presence.visible_lessons_clause(user))
             .first())
    if event is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return event


@router.get("/lessons/{event_id}")
def get_lesson_record(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    return meet_presence.lesson(db, _visible_lesson(db, current_user, event_id))


class IdentityIn(BaseModel):
    """Who a Meet account is: an LMS person, "not a student", or — both empty — unknown again."""
    user_id: Optional[int] = None
    not_a_student: bool = False


@router.put("/participants/{participant_id}")
def confirm_identity(
    participant_id: int,
    body: IdentityIn,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Say who an account in this lesson is. For a signed-in Google account that answer is
    remembered for every lesson; a guest is matched for this lesson only. Returns the
    lesson's record as it reads now."""
    participant = db.get(MeetParticipant, participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="Not found")
    event = _visible_lesson(db, current_user, participant.event_id)

    if body.user_id is not None and body.not_a_student:
        raise HTTPException(status_code=422, detail="Choose a person or 'not a student', not both")
    if body.user_id is not None and body.user_id not in meet_presence.candidate_ids(db, event):
        raise HTTPException(status_code=422, detail="Choose the teacher or a student of this lesson")

    clearing = body.user_id is None and not body.not_a_student
    if participant.kind == "signed_in" and participant.google_user:
        link = db.get(GoogleAccountLink, participant.google_user)
        if clearing:
            if link is not None:
                db.delete(link)
        else:
            if link is None:
                link = GoogleAccountLink(google_user=participant.google_user)
                db.add(link)
            link.user_id = body.user_id
            link.not_a_student = body.not_a_student
            link.display_name = participant.display_name
            link.confirmed_by = current_user.id
            link.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        participant.lesson_user_id = body.user_id
        participant.lesson_not_a_student = body.not_a_student
    db.commit()
    return meet_presence.lesson(db, event)


@router.get("/lessons")
def list_lesson_records(
    date_from: Optional[datetime] = Query(None, description="UTC; default 30 days before date_to"),
    date_to: Optional[datetime] = Query(None, description="UTC; default now"),
    teacher_id: Optional[int] = None,
    group_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Every lesson with a Meet record in the range, newest first, with its flags.

    One endpoint for two screens: the review list (no filters) and the attendance journal's
    warning dots (``group_id`` and the journal's dates)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    date_to = _utc_naive(date_to) or now
    date_from = _utc_naive(date_from) or date_to - timedelta(days=DEFAULT_DAYS)
    date_from = max(date_from, date_to - timedelta(days=MAX_DAYS))

    has_record = exists().where(and_(MeetConference.event_id == Event.id,
                                     MeetConference.synced_at.isnot(None))).correlate(Event)
    query = db.query(Event).filter(
        meet_presence.visible_lessons_clause(current_user),
        Event.start_datetime >= date_from,
        Event.start_datetime < date_to,
        has_record,
    )
    if teacher_id is not None:
        query = query.filter(Event.teacher_id == teacher_id)
    if group_id is not None:
        query = query.filter(exists().where(and_(EventGroup.event_id == Event.id,
                                                 EventGroup.group_id == group_id)).correlate(Event))
    events = query.order_by(Event.start_datetime.desc()).limit(MAX_LESSONS).all()

    groups: dict = {}
    for event_id, gid, name in (db.query(EventGroup.event_id, Group.id, Group.name)
                                .join(Group, Group.id == EventGroup.group_id)
                                .filter(EventGroup.event_id.in_([e.id for e in events] or [-1]))):
        groups.setdefault(event_id, []).append({"id": gid, "name": name})

    items = []
    for record in meet_presence.records(db, events, now):
        teacher = record.get("teacher")
        students = record.get("students") or []
        items.append({
            "event_id": record["event_id"],
            "title": record["title"],
            "start": record["start"],
            "end": record["end"],
            "state": record["state"],
            "groups": sorted(groups.get(record["event_id"], []), key=lambda g: g["name"]),
            "teacher": {"id": teacher["user_id"], "name": teacher["name"],
                        "first_join": teacher["first_join"], "last_leave": teacher["last_leave"]} if teacher else None,
            "students": len(students),
            "joined": sum(1 for s in students if s["sessions"]),
            "unknown": len(record.get("unknown") or []),
            "held_back": record.get("held_back", False),
            "mismatches": record.get("mismatches", 0),
            "flags": record.get("flags") or [],
        })
    return {"items": items, "from": utc_z(date_from), "to": utc_z(date_to)}
