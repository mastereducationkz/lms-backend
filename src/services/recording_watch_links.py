"""Login-free links to one lesson's recording, for people who have no LMS account.

Accountants apply "no recording, no pay" from the CRM and have no LMS login. The CRM decides
who may watch — whoever can see that lesson on the CRM screen they are on — and asks for a
link over its service channel (``X-CRM-Service-Key``). A link:

* opens exactly one lesson's recording, through the same prefix-scoped media token the LMS
  player uses (``media_tokens.signed_hls_url``), so it can never reach another lesson's files;
* stops working three hours after it was made (owner, 2026-09-11) — enough to watch a lesson
  with pauses, short enough that a forwarded link dies the same day; opening it again from the
  CRM makes a new one;
* is stored only as a SHA-256 hash, with who asked for it and when it was opened.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.schemas.models import Event, EventGroup, Group, LessonRecording, RecordingWatchLink, UserInDB
from src.services.media_tokens import signed_hls_url
from src.services.recording_access import public_status
from src.utils.utc_json import utc_z

LINK_TTL = timedelta(hours=3)

# The media token names a user for its audit trail only; a watch link has no LMS user.
WATCH_LINK_VIEWER_ID = 0

_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class NothingToWatch(LookupError):
    """No such link, or the lesson has no playable recording (never had one, or it was retired)."""


class LinkExpired(Exception):
    """The link was real and has run out; the CRM can make a new one."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lms_url(path: str) -> str:
    """The LMS site's own address — the same setting email links use (``LMS_URL``)."""
    base = (os.getenv("LMS_URL") or "").strip().rstrip("/") or "https://lms.mastereducation.kz"
    return f"{base}{path}"


def _playable(db, event_id: int) -> LessonRecording:
    recording = db.query(LessonRecording).filter(LessonRecording.event_id == event_id).first()
    if recording is None or public_status(recording) != "ready":
        raise NothingToWatch(f"lesson {event_id} has no recording to watch")
    return recording


def issue(db, event_id: int, *, issued_to: Optional[str], issued_role: Optional[str],
          now: Optional[datetime] = None) -> dict:
    """Make a link to one lesson's recording. The caller has already decided who may watch."""
    _playable(db, event_id)
    now = now or _utcnow()
    token = secrets.token_urlsafe(32)
    link = RecordingWatchLink(token_hash=_hash(token), event_id=event_id, issued_to=issued_to,
                              issued_role=issued_role, created_at=now, expires_at=now + LINK_TTL)
    db.add(link)
    db.commit()
    return {"url": lms_url(f"/watch/{token}"), "expires_at": utc_z(link.expires_at)}


def redeem(db, token: str, now: Optional[datetime] = None) -> dict:
    """What the watch page needs, for a link that is real and still running. Counts the open."""
    if not _TOKEN_SHAPE.match(token or ""):
        raise NothingToWatch("malformed link")
    link = db.query(RecordingWatchLink).filter(RecordingWatchLink.token_hash == _hash(token)).first()
    if link is None:
        raise NothingToWatch("unknown link")
    now = now or _utcnow()
    if now >= link.expires_at:
        raise LinkExpired()
    recording = _playable(db, link.event_id)

    link.open_count = int(link.open_count or 0) + 1
    link.first_opened_at = link.first_opened_at or now
    link.last_opened_at = now
    db.commit()

    event = db.get(Event, link.event_id)
    teacher = db.get(UserInDB, event.teacher_id) if event and event.teacher_id else None
    groups = [name for (name,) in db.query(Group.name).join(EventGroup, EventGroup.group_id == Group.id)
              .filter(EventGroup.event_id == link.event_id).order_by(Group.name)]
    return {
        "title": event.title if event else "Урок",
        "start": utc_z(event.start_datetime) if event else None,
        "end": utc_z(event.end_datetime) if event else None,
        "teacher": teacher.name if teacher else None,
        "groups": groups,
        "duration_seconds": recording.duration_seconds,
        "url": signed_hls_url(recording.hls_url, WATCH_LINK_VIEWER_ID),
        "poster_url": signed_hls_url(recording.poster_url, WATCH_LINK_VIEWER_ID),
        "expires_at": utc_z(link.expires_at),
    }
