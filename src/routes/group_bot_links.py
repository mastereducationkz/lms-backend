"""``GET /tg/l/<group>-<sig>`` — the 🔗 Урок button: the Meet room of the lesson that matters now.

Public on purpose: a student taps it in Telegram, logged in to nothing. What it can reveal is the
link the bot posts into the same chat five minutes before every lesson, and the signature
(:func:`group_bot_keyboard.lesson_link_sig`) keeps it to that group's chat. Nothing is written.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from src.config import get_db
from src.schemas.models import Group
from src.services import group_bot, group_bot_keyboard
from src.services.recording_watch_links import lms_url

router = APIRouter()

LOOKAHEAD = 2      # the lesson underway, then the next one


def lesson_target(db, group: Group, now: datetime) -> str:
    """The running lesson's room, else the next lesson's, else the LMS calendar."""
    lessons = group_bot._lessons(db, group, now).limit(LOOKAHEAD).all()
    running = [lesson for lesson in lessons if lesson.start_datetime <= now]
    upcoming = [lesson for lesson in lessons if lesson.start_datetime > now]
    for lesson in running[:1] + upcoming[:1]:
        url = (lesson.meeting_url or "").strip()
        if url.startswith("https://"):
            return url
    return lms_url("/calendar")


@router.get("/l/{token}")
def open_group_lesson(token: str, db: Session = Depends(get_db)):
    group_id = group_bot_keyboard.parse_lesson_token(token)
    group = db.get(Group, group_id) if group_id is not None else None
    if group is None:
        raise HTTPException(status_code=404, detail="Not found")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return RedirectResponse(lesson_target(db, group, now), status_code=302)
