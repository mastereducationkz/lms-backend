"""Who was in a lesson's Meet room, from when to when — and the one-time "who is this account".

Read by admins, head curators and head teachers (every lesson), teachers (their lessons) and
curators (their groups' lessons); students never (owner, 2026-09-11). Anyone else gets 404,
not 403, the same as recordings. The meaning of the record lives in ``meet_presence``; this
file only checks access and shapes requests.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
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
    MeetFlagReview,
    MeetParticipant,
    UserInDB,
)
from src.services import meet_presence, meet_talk, talk_settings
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


class ReviewIn(BaseModel):
    """Mark one flag reviewed: with a reason, or — for a mark that contradicts the room — by
    correcting the mark itself (then the flag is gone and there is nothing left to review)."""
    user_id: int
    code: str = Field(max_length=40)
    reason_code: Optional[str] = Field(None, max_length=40)
    reason_text: Optional[str] = Field(None, max_length=500)
    fix_mark: bool = False


# The journal's own marking rule (/leaderboard/curator/attendance/bulk): curators read marks but
# never write them; a teacher writes their own groups'; a head teacher the groups they oversee.
def _can_mark(db, user, event: Event) -> bool:
    if user.role in ("admin", "head_curator"):
        return True
    group_ids = [gid for (gid,) in db.query(EventGroup.group_id).filter(EventGroup.event_id == event.id)]
    if user.role == "teacher":
        return db.query(Group.id).filter(Group.id.in_(group_ids or [-1]), Group.teacher_id == user.id).first() is not None
    if user.role == "head_teacher":
        from src.gamification.routes.leaderboard import head_teacher_can_access_group

        return any(head_teacher_can_access_group(db, user.id, gid) for gid in group_ids)
    return False


def _flag_on(record: dict, user_id: int, code: str) -> Optional[dict]:
    return next((f for f in record.get("flags") or [] if f["user_id"] == user_id and f["code"] == code), None)


def _may_review(user, code: str) -> None:
    if code in meet_presence.TEACHER_FLAGS and user.role not in meet_presence.TEACHER_FLAG_REVIEWERS:
        raise HTTPException(status_code=403, detail="Only admins and heads can review a teacher's own flags")


@router.put("/lessons/{event_id}/reviews")
def review_flag(
    event_id: int,
    body: ReviewIn,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Take one flag out of «Needs attention». Returns the lesson's record as it now reads."""
    event = _visible_lesson(db, current_user, event_id)
    record = meet_presence.lesson(db, event)
    if record["state"] != "ready":
        raise HTTPException(status_code=409, detail="This lesson's Meet record is not complete yet")
    flag = _flag_on(record, body.user_id, body.code)
    if flag is None:
        raise HTTPException(status_code=404, detail="That flag is not on this lesson any more")
    _may_review(current_user, body.code)

    if body.fix_mark:
        if body.code not in ("marked_present_not_joined", "marked_absent_was_in_room"):
            raise HTTPException(status_code=422, detail="Only a mark that contradicts the room can be corrected here")
        if not _can_mark(db, current_user, event):
            raise HTTPException(status_code=403, detail="You cannot change marks on this lesson")
        from src.services.attendance_service import AttendanceService

        if body.code == "marked_present_not_joined":
            status, score = "absent", 0
        else:
            late = _flag_on(record, body.user_id, "late")
            status, score = ("late" if late else "present"), 1
        # The journal's own write: the same row, the same status vocabulary, the same billing.
        AttendanceService.upsert_for_event(db=db, event_id=event.id, user_id=body.user_id, status=status, score=score)
        db.commit()
        return meet_presence.lesson(db, event)

    allowed = {key for key, _ in meet_presence.REVIEW_REASONS.get(body.code, [])} | {meet_presence.OTHER_REASON[0]}
    text = (body.reason_text or "").strip() or None
    if body.reason_code is None and body.code in meet_presence.REASON_REQUIRED:
        raise HTTPException(status_code=422, detail="Choose a reason: this mark contradicts the room")
    if body.reason_code is not None and body.reason_code not in allowed:
        raise HTTPException(status_code=422, detail="Unknown reason")
    if body.reason_code == meet_presence.OTHER_REASON[0] and not text:
        raise HTTPException(status_code=422, detail="Describe the reason")

    review = (db.query(MeetFlagReview)
              .filter_by(event_id=event.id, user_id=body.user_id, code=body.code).first())
    if review is None:
        review = MeetFlagReview(event_id=event.id, user_id=body.user_id, code=body.code)
        db.add(review)
    review.reason_code = body.reason_code
    review.reason_text = text
    review.reviewed_by = current_user.id
    review.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return meet_presence.lesson(db, event)


@router.delete("/lessons/{event_id}/reviews")
def restore_flag(
    event_id: int,
    user_id: int,
    code: str,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Put a reviewed flag back into «Needs attention» (the «Показать отмеченные» undo)."""
    event = _visible_lesson(db, current_user, event_id)
    _may_review(current_user, code)
    db.query(MeetFlagReview).filter_by(event_id=event.id, user_id=user_id, code=code).delete()
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

    records, batch = meet_presence.records_with_batch(db, events, now)
    talk_on = talk_settings.enabled(db)
    talk = meet_talk.summaries(db, events, records, batch, now) if talk_on else {}
    items = []
    for record in records:
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
            "reviewed": record.get("reviewed", 0),
            "flags": record.get("flags") or [],
            "talk": talk.get(record["event_id"]),
        })
    return {"items": items, "from": utc_z(date_from), "to": utc_z(date_to),
            "review_options": meet_presence.review_options(), "talk_enabled": talk_on}
