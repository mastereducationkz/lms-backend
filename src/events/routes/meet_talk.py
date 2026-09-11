"""Talk time: who spoke in a lesson and for how long, a group's totals, and the LMS switch.

Read by exactly the people who read Meet attendance (``meet_presence.visible_lessons_clause``):
admins and heads every lesson, teachers their lessons, curators their groups' — never students
(owner, 2026-09-11). Anyone else gets 404. The meaning lives in ``meet_talk``; the switch in
``talk_settings``.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import get_db
from src.events.routes.meet_attendance import DEFAULT_DAYS, MAX_DAYS, _utc_naive, _visible_lesson
from src.routes.auth import get_current_user_dependency
from src.schemas.models import Group, UserInDB
from src.services import meet_talk, meet_talk_stats, talk_settings
from src.services.meet_presence import RECORD_ROLES

router = APIRouter()


@router.get("/lessons/{event_id}/talk")
def get_lesson_talk(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    return meet_talk.lesson_talk(db, _visible_lesson(db, current_user, event_id), viewer_role=current_user.role)


@router.get("/talk/groups/{group_id}")
def get_group_talk(
    group_id: int,
    date_from: Optional[datetime] = Query(None, description="UTC; default 30 days before date_to"),
    date_to: Optional[datetime] = Query(None, description="UTC; default now"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Every student of one group, added up over the period — the «Talk time» tab."""
    group = db.get(Group, group_id)
    if group is None or current_user.role not in RECORD_ROLES \
            or not meet_talk_stats.may_see_group(db, current_user, group):
        raise HTTPException(status_code=404, detail="Group not found")
    if not talk_settings.enabled(db):
        raise HTTPException(status_code=409, detail="Talk time is switched off")
    date_from, date_to, now = _range(date_from, date_to)
    return meet_talk_stats.group_talk(db, current_user, group, date_from, date_to, now=now)


def _range(date_from: Optional[datetime], date_to: Optional[datetime]) -> tuple:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    date_to = _utc_naive(date_to) or now
    date_from = _utc_naive(date_from) or date_to - timedelta(days=DEFAULT_DAYS)
    return max(date_from, date_to - timedelta(days=MAX_DAYS * 2)), date_to, now


def _heads_only(db, user) -> None:
    """Every teacher side by side is for heads (owner, 2026-09-11) — and only while switched on."""
    if user.role not in meet_talk_stats.HEADS:
        raise HTTPException(status_code=404, detail="Not found")
    if not talk_settings.enabled(db):
        raise HTTPException(status_code=409, detail="Talk time is switched off")


@router.get("/talk/teachers")
def get_teachers_talk(
    date_from: Optional[datetime] = Query(None, description="UTC; default 30 days before date_to"),
    date_to: Optional[datetime] = Query(None, description="UTC; default now"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Every teacher over the period: their share, stretches, questions, silent students."""
    _heads_only(db, current_user)
    date_from, date_to, now = _range(date_from, date_to)
    return meet_talk_stats.teachers_talk(db, current_user, date_from, date_to, now=now)


@router.get("/talk/teachers/{teacher_id}")
def get_teacher_talk(
    teacher_id: int,
    date_from: Optional[datetime] = Query(None, description="UTC; default 30 days before date_to"),
    date_to: Optional[datetime] = Query(None, description="UTC; default now"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """One teacher over the period: all their groups together, each group, each lesson."""
    _heads_only(db, current_user)
    date_from, date_to, now = _range(date_from, date_to)
    out = meet_talk_stats.teacher_talk(db, current_user, teacher_id, date_from, date_to, now=now)
    if out is None:
        raise HTTPException(status_code=404, detail="Teacher not found")
    return out


class TalkSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    transcripts: Optional[bool] = None


@router.get("/talk/settings")
def get_talk_settings(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    if current_user.role not in talk_settings.READERS:
        raise HTTPException(status_code=404, detail="Not found")
    return talk_settings.describe(db)


@router.put("/talk/settings")
def put_talk_settings(
    body: TalkSettingsIn,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """The admin's switch. On: Meet transcribes every LMS lesson room from the next lesson on
    (the worker sets the rooms within five minutes). Off: it stops; saved talk time stays."""
    if current_user.role not in talk_settings.WRITERS:
        raise HTTPException(status_code=403, detail="Only admins can switch talk time on or off")
    talk_settings.update(db, current_user, enabled=body.enabled, transcripts=body.transcripts)
    return talk_settings.describe(db)
