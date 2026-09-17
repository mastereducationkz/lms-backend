"""Close a lesson's Meet room that is still open after the lesson, once the teacher has gone.

A student who forgets to leave keeps the call alive. While it lives, Meet has not finished the
conference, so the lesson's attendance record and its recording both wait — on 11.09 the 19:00
lesson sat open past 20:07 with one student in it. The robot created every lesson room, so it
may end the call for everyone (``spaces.endActiveConference``); the room itself stays and can
be rejoined.

The rule (owner, 2026-09-11): 15 minutes after the lesson's scheduled end, and only when the
teacher is no longer in the room — a teacher running over is never cut off. If none of the
teacher's Google accounts has been confirmed yet, their presence cannot be seen, so the room
waits 60 minutes instead. ``ENABLE_AUTO_CLOSE_ROOMS=false`` switches it off.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config import SessionLocal
from src.schemas.models import GoogleAccountLink
from src.services import google_workspace, meet_recordings

logger = logging.getLogger(__name__)

CLOSE_AFTER = timedelta(minutes=15)
CLOSE_AFTER_UNSEEN_TEACHER = timedelta(minutes=60)
POLL_INTERVAL_SECONDS = 60


def enabled() -> bool:
    return os.getenv("ENABLE_AUTO_CLOSE_ROOMS", "true").strip().lower() not in ("0", "false", "no", "off")


def _pages(call, key: str, **kwargs) -> list:
    items, token = [], None
    while True:
        response = call(pageSize=100, pageToken=token, **kwargs).execute()
        items.extend(response.get(key, []))
        token = response.get("nextPageToken")
        if not token:
            return items


def close_lingering_rooms(db, now: Optional[datetime] = None) -> int:
    """End every live lesson call the rule says is over. Returns how many were closed."""
    if not enabled():
        return 0
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    meet = google_workspace.meet_client()
    closed = 0
    for conference in _pages(meet.conferenceRecords().list, "conferenceRecords", filter="end_time IS NULL"):
        space = conference.get("space")
        try:
            lesson = meet_recordings.match_lesson(db, meet_recordings.space_meet_code(space or ""))
            if lesson is None or lesson.end_datetime is None:
                continue  # not a lesson room
            overdue = now - lesson.end_datetime
            if overdue < CLOSE_AFTER:
                continue
            teacher_accounts = {google_user for (google_user,) in db.query(GoogleAccountLink.google_user)
                                .filter(GoogleAccountLink.user_id == lesson.teacher_id)}
            lesson_id = lesson.id
            db.commit()  # nothing held open on the database while Google answers

            still_in = _pages(meet.conferenceRecords().participants().list, "participants",
                              parent=conference["name"], filter="latest_end_time IS NULL")
            if any((p.get("signedinUser") or {}).get("user") in teacher_accounts for p in still_in):
                continue  # the teacher is still teaching
            if not teacher_accounts and overdue < CLOSE_AFTER_UNSEEN_TEACHER:
                continue  # cannot see whether the teacher is there yet: give it an hour
            meet.spaces().endActiveConference(name=space, body={}).execute()
            closed += 1
            logger.info("lesson %s: closed its Meet room %d min after the end (%d still in, not the teacher)",
                        lesson_id, int(overdue.total_seconds() // 60), len(still_in))
        except Exception as e:
            db.rollback()
            logger.warning("closing room %s: %s", space, e)
    return closed


class MeetRoomCloserWorker:
    """Poll live Meet rooms independently of recording ingestion.

    Recording conversion and transcription can take minutes. This worker has a
    deliberately narrow responsibility, so a student left in a room is removed
    promptly even while the recording pipeline is busy.
    """

    def __init__(self, poll_interval: int | str = POLL_INTERVAL_SECONDS):
        try:
            configured_interval = int(poll_interval)
        except (TypeError, ValueError):
            configured_interval = POLL_INTERVAL_SECONDS
            logger.warning("Invalid Meet room-closer poll interval %r; using %ss", poll_interval, configured_interval)
        self.poll_interval = configured_interval if configured_interval > 0 else POLL_INTERVAL_SECONDS
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        # Room safety only needs Meet OAuth; it remains available if recording
        # ingestion is deliberately switched off.
        if not google_workspace.oauth_configured():
            logger.info("Meet room closer disabled (Google Meet OAuth is not configured)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="meet-room-closer", daemon=True)
        self._thread.start()
        logger.info("Meet room closer started (poll=%ss)", self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(5, self.poll_interval + 5))

    def tick(self) -> int:
        closed = self._run(close_lingering_rooms, "Meet room closer")
        # Same live rooms, same minute: staff hear about a lesson going wrong while it runs.
        from src.services import meet_staff_notices
        self._run(meet_staff_notices.run, "Meet staff notices")
        return closed

    @staticmethod
    def _run(step, label: str) -> int:
        db = SessionLocal()
        try:
            return step(db)
        except Exception as e:
            db.rollback()
            logger.error("%s tick failed: %s", label, e, exc_info=True)
            return 0
        finally:
            db.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_interval)
