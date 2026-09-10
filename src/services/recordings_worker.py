"""The lesson-recording pipeline's scheduler loop.

Four jobs on a timer, in the order a lesson actually moves through them:

1. give upcoming lessons a Meet link,
2. notice conferences that have finished and claim their recordings,
3. turn one claimed recording into HLS on S3,
4. flag lessons that ended with no recording, before payroll runs.

Each step is independent and each swallows its own exceptions. A Calendar outage must not
stop ingest; a bad recording must not stop tomorrow's lessons getting links. The loop
never dies — that is the same shape ``VideoIngestWorker`` uses, for the same reason: this
runs unattended in the scheduler container.

Off by default. ``ENABLE_RECORDINGS`` gates the whole thing, exactly as
``ENABLE_VIDEO_INGEST`` gates video ingest.
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config import SessionLocal
from src.schemas.models import Event, LessonRecording, UserInDB
from src.services import (
    google_workspace,
    meet_recordings,
    meet_scheduling,
    recording_alerts,
    recording_ingest,
)

logger = logging.getLogger(__name__)

# How far ahead to create Meet links. Far enough that a teacher opening tomorrow's
# calendar sees the lesson; short enough that a fortnight of reschedules does not leave a
# trail of abandoned conferences.
SCHEDULE_HORIZON_DAYS = 3

POLL_INTERVAL_SECONDS = 300


def _horizon() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=SCHEDULE_HORIZON_DAYS)


def ensure_upcoming_meet_links(db, limit: int = 50) -> int:
    """Give Meet links to soon-starting lessons whose teacher is onboarded.

    The ``workspace_email IS NOT NULL`` join is the rollout switch: with one teacher
    onboarded this touches only that teacher's lessons, however many thousands of other
    lessons are scheduled.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lessons = (
        db.query(Event)
        .join(UserInDB, UserInDB.id == Event.teacher_id)
        .filter(
            Event.is_active.is_(True),
            Event.meeting_url.is_(None),
            Event.start_datetime > now,
            Event.start_datetime < _horizon(),
            UserInDB.workspace_email.isnot(None),
        )
        .order_by(Event.start_datetime)
        .limit(limit)
        .all()
    )

    created = 0
    for lesson in lessons:
        try:
            if meet_scheduling.ensure_meet_link(db, lesson):
                created += 1
        except Exception as e:
            # One lesson failing (a deleted teacher, a quota blip) must not stop the rest.
            db.rollback()
            logger.warning("lesson %s: could not create Meet link: %s", lesson.id, e)
    return created


def poll_for_recordings(db) -> int:
    """Claim recordings for conferences that have finished."""
    claimed = 0
    for conference in meet_recordings.list_recent_conferences():
        name = conference.get("name")
        space = conference.get("space")
        if not name or not space:
            continue
        try:
            code = meet_recordings.space_meet_code(space)
            lesson = meet_recordings.match_lesson(db, code)
            if lesson is None:
                # Someone's ad-hoc meeting, or a lesson from before the pilot. Not ours.
                continue
            drive_file_id = meet_recordings.resolve_recording(name)
            if meet_recordings.claim_recording(db, lesson, name, drive_file_id):
                claimed += 1
        except meet_recordings.RecordingNotReady:
            # Normal for a lesson that just ended: Meet publishes the conference before
            # the file. Nothing to log at warning level; we look again next tick.
            continue
        except Exception as e:
            db.rollback()
            logger.warning("conference %s: %s", name, e)
    return claimed


def ingest_one_pending(db) -> bool:
    """Transcode and upload a single claimed recording. True if one was processed.

    One per tick on purpose: transcoding is CPU-heavy and this container shares four
    cores with the rest of the stack.
    """
    recording = (
        db.query(LessonRecording)
        .filter(
            LessonRecording.status == "pending",
            LessonRecording.drive_file_id.isnot(None),
            LessonRecording.attempts < recording_ingest.MAX_ATTEMPTS,
        )
        .order_by(LessonRecording.created_at)
        .first()
    )
    if recording is None:
        return False

    recording.attempts += 1
    db.commit()
    try:
        recording_ingest.process_recording(db, recording)
    except Exception as e:
        db.rollback()
        recording_ingest.fail_recording(db, recording, e)
    return True


class RecordingsWorker:
    def __init__(self, poll_interval: int = POLL_INTERVAL_SECONDS):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not google_workspace.recordings_enabled():
            logger.info("Recordings pipeline disabled (ENABLE_RECORDINGS/OAuth env) — not starting")
            return
        self._thread = threading.Thread(target=self._loop, name="recordings", daemon=True)
        self._thread.start()
        logger.info("🎥 Recordings worker started (poll=%ss)", self.poll_interval)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # never let the loop die
                logger.error("Recordings loop error: %s", e, exc_info=True)
            self._stop.wait(self.poll_interval)

    def tick(self) -> dict:
        """One pass. Returns a summary, which makes it directly testable and callable by hand."""
        db = SessionLocal()
        summary = {"links": 0, "claimed": 0, "ingested": False, "missing": 0}
        try:
            for key, fn in (
                ("links", ensure_upcoming_meet_links),
                ("claimed", poll_for_recordings),
                ("ingested", ingest_one_pending),
                ("missing", recording_alerts.sweep_missing_recordings),
            ):
                try:
                    summary[key] = fn(db)
                except Exception as e:
                    db.rollback()
                    logger.error("recordings tick step %s failed: %s", key, e, exc_info=True)
            return summary
        finally:
            db.close()
