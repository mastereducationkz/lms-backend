"""Watch a lesson recording.

Who may watch is decided in one place, ``src/services/recording_access.py`` — the same rule
the Recordings library and the calendar ask ("own lessons only", 2026-09-10). In short: the
lesson's students (attended or not), its teacher and the owner of its group, its group's
curator, and head curators, head teachers and admins. A student in another group gets 404,
not 403 — a 403 would confirm the recording exists.

Video is never served from Google Drive: Drive's quota makes it unusable for concurrent
viewers, and the LMS already owns a working signed-HLS path (§4.4).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import Event, LessonRecording, UserInDB
from src.services.media_tokens import signed_hls_url
from src.services.recording_access import may_watch, public_status

logger = logging.getLogger(__name__)

router = APIRouter()

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

    if not may_watch(db, current_user, event):
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

    return playback_payload(recording, current_user.id)


def playback_payload(recording: LessonRecording, viewer_id: int) -> dict:
    """What a viewer gets for a recording they may watch. Tokens are minted for ``viewer_id``.

    pending and failed surface honestly rather than as a 404, so the UI can say "being
    processed". A ready row whose video was removed by retention says so, instead of
    offering a Watch button that plays nothing.
    """
    state = public_status(recording)
    if state != "ready":
        return {"status": state, "url": None}
    return {
        "status": "ready",
        "url": signed_hls_url(recording.hls_url, viewer_id),
        "poster_url": signed_hls_url(recording.poster_url, viewer_id),
        "duration_seconds": recording.duration_seconds,
    }
