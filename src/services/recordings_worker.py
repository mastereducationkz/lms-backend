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

# How long after a lesson's scheduled end to wait before choosing among its recordings.
# Lessons overrun, and a lesson still in progress has not produced its real recording yet
# — the only candidate would be an early-joiner's empty room.
SETTLE_MINUTES = 20

# How long to keep waiting for a sibling recording that is still rendering before giving
# up and claiming the best one already in hand. Long enough for Meet to finish a
# feature-length lesson; short enough that one stuck render cannot strand a lesson.
PATIENCE_HOURS = 4


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
    """Claim, for each finished lesson, the recording that is actually the lesson.

    **One lesson can produce several recordings.** A Meet space opens a new conference
    every time the room goes from empty to occupied, and with auto-recording each one
    produces its own finished file. A student who joins fifteen minutes early and leaves
    again generates a complete recording of an empty room — and because it ends first, it
    also lands in Drive first.

    Claiming the first file to appear therefore claims the wrong one, and
    ``claim_recording`` is idempotent per lesson, so the real recording that arrives an
    hour later is silently discarded. The lesson is lost with no error anywhere: the row
    reads ``ready``, and students get two minutes of an empty room.

    So we do not claim per conference. We group every conference by the lesson it belongs
    to, wait until the lesson is over and its recordings have settled, and then claim the
    **longest** one — the only one of them that can be an hour-long lesson.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # lesson id -> (lesson, [conference names])
    by_lesson: dict = {}
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
            by_lesson.setdefault(lesson.id, (lesson, []))[1].append(name)
        except Exception as e:
            logger.warning("conference %s: %s", name, e)

    claimed = 0
    for lesson, names in by_lesson.values():
        try:
            if _claim_best_recording(db, lesson, names, now):
                claimed += 1
        except Exception as e:
            db.rollback()
            logger.warning("lesson %s: %s", lesson.id, e)
    return claimed


def _claim_best_recording(db, lesson, conference_names: list, now: datetime) -> bool:
    """Pick the longest finished recording for one lesson and claim it."""
    if db.query(LessonRecording).filter(LessonRecording.event_id == lesson.id).first():
        return False

    # Don't choose while the lesson may still be running: the real recording does not
    # exist yet, so the only candidate would be an early-joiner's empty room.
    if lesson.end_datetime and now < lesson.end_datetime + timedelta(minutes=SETTLE_MINUTES):
        return False

    best, pending = None, False
    for name in conference_names:
        try:
            file_id, seconds = meet_recordings.resolve_recording_detail(name)
        except meet_recordings.RecordingNotReady:
            # Meet publishes a conference before it finishes rendering the file.
            pending = True
            continue
        if best is None or seconds > best[2]:
            best = (name, file_id, seconds)

    if best is None:
        return False

    # If some sibling is still rendering it might be the real lesson, so normally wait
    # rather than lock in a shorter one. Not forever: a recording that never materialises
    # must not block the one we already have.
    if pending and now < lesson.end_datetime + timedelta(hours=PATIENCE_HOURS):
        logger.info("lesson %s: %s conference(s) still rendering — waiting before claiming",
                    lesson.id, len(conference_names))
        return False

    name, file_id, seconds = best
    if len(conference_names) > 1:
        logger.info("lesson %s: %s conferences, claiming the longest (%.0fs) — %s",
                    lesson.id, len(conference_names), seconds, name)
    return bool(meet_recordings.claim_recording(db, lesson, name, file_id))


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
