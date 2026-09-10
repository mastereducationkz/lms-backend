"""Watch a lesson recording.

The access rule (spec §4.4, §4.9):

* every student in the lesson's group may watch, **whether or not they attended** —
  rewatching a missed lesson is the primary value, so attendance is deliberately not a
  condition;
* the lesson's own teacher may watch;
* curators, head teachers and admins may watch, for quality review, and teachers are told
  this in writing before launch (§4.9);
* nobody else. A student in another group gets 404, not 403 — a 403 would confirm the
  recording exists.

Video is never served from Google Drive: Drive's quota makes it unusable for concurrent
viewers, and the LMS already owns a working signed-HLS path (§4.4).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import (
    Event, EventGroup, GroupStudent, LessonRecording, UserInDB,
)
from src.services.media_tokens import signed_hls_url

logger = logging.getLogger(__name__)

router = APIRouter()

STAFF_ROLES = {"curator", "head_curator", "teacher", "head_teacher", "admin"}


def _may_watch(db: Session, user: UserInDB, event: Event) -> bool:
    if user.role == "admin":
        return True
    if event.teacher_id == user.id:
        return True
    if user.role in STAFF_ROLES:
        # Curators and head teachers review quality across groups (§4.9).
        return True
    # Students: membership of any group this lesson was taught to. Attendance is
    # deliberately not checked — see the module docstring.
    return db.query(GroupStudent).join(
        EventGroup, EventGroup.group_id == GroupStudent.group_id
    ).filter(
        EventGroup.event_id == event.id,
        GroupStudent.student_id == user.id,
    ).first() is not None


@router.get("/{event_id}/recording")
def get_lesson_recording(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """The lesson's recording, with a freshly signed playback URL.

    The URL is minted per request and per viewer. It must never be cached: the token is
    scoped to the caller, so a cached response would hand one viewer's token to everyone
    else for the lifetime of the cache. This endpoint is deliberately absent from the
    cache-invalidation rules for that reason.
    """
    event = db.query(Event).filter(Event.id == event_id).first()
    if event is None:
        raise HTTPException(status_code=404, detail="Lesson not found")

    if not _may_watch(db, current_user, event):
        # 404 rather than 403: do not confirm that a recording exists to someone who may
        # not see it.
        raise HTTPException(status_code=404, detail="Recording not found")

    recording = (
        db.query(LessonRecording)
        .filter(LessonRecording.event_id == event_id)
        .first()
    )
    if recording is None:
        return {"status": "missing", "url": None}

    if recording.status != "ready" or not recording.hls_url:
        # pending / failed / purged all surface honestly rather than as a 404, so the UI
        # can say "being processed" instead of "does not exist".
        return {"status": recording.status, "url": None}

    return {
        "status": "ready",
        "url": signed_hls_url(recording.hls_url, current_user.id),
        "duration_seconds": recording.duration_seconds,
    }
