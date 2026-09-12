"""The lesson-recording pipeline's scheduler loop.

Four jobs on a timer, in the order a lesson actually moves through them:

1. give upcoming lessons a Meet link,
2. notice conferences that have finished and claim their recordings,
3. make claimed recordings streamable (HLS on S3), as many as fit in a tick,
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
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config import SessionLocal
from src.schemas.models import Event, LessonRecording, UserInDB
from src.services import (
    google_workspace,
    meet_attendance,
    meet_recordings,
    meet_room_closer,
    meet_scheduling,
    meet_talk_sync,
    recording_alerts,
    recording_ingest,
)
from src.services.operational_groups import event_belongs_on_calendar_clause

logger = logging.getLogger(__name__)

# How far ahead to create Meet links. Far enough that a teacher opening tomorrow's
# calendar sees the lesson; short enough that a fortnight of reschedules does not leave a
# trail of abandoned conferences.
SCHEDULE_HORIZON_DAYS = 3

# A breath between new rooms: Meet allows 100 a minute per account, and a burst of them also
# starves the space patches that follow each one.
ROOM_CREATE_PAUSE = 0.5

POLL_INTERVAL_SECONDS = 300

# How long one tick keeps starting new ingests before handing back to links and polling.
INGEST_BUDGET_SECONDS = 240

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
            # No room for a lesson the calendar does not show: a switched-off group's leftover
            # lessons were being given Meet links and invites on the teacher's calendar.
            event_belongs_on_calendar_clause(now),
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
                time.sleep(ROOM_CREATE_PAUSE)
        except Exception as e:
            # One lesson failing (a deleted teacher, a quota blip) must not stop the rest.
            db.rollback()
            if rate_limited(e):
                # Google allows so many new rooms a minute (100 per user). Four teachers joining
                # the pilot at once asked for 27 at once; the rest are made on the next ticks,
                # long before their lessons.
                logger.info("Meet's room quota is spent for now — %s made, the rest next tick", created)
                break
            logger.warning("lesson %s: could not create Meet link: %s", lesson.id, e)
    return created


def rate_limited(error: Exception) -> bool:
    """Google saying "too many, too fast" — the one failure worth pausing the whole loop for."""
    return getattr(getattr(error, "resp", None), "status", None) == 429 or "429" in str(error)[:120]


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

    best, rendering = None, 0
    for name in conference_names:
        try:
            file_id, seconds = meet_recordings.resolve_recording_detail(name)
        except meet_recordings.NoRecording:
            # Students alone in the room, or a test call: nothing was recorded, nothing will be.
            continue
        except meet_recordings.RecordingNotReady:
            # Meet publishes a conference before it finishes rendering the file.
            rendering += 1
            continue
        if best is None or seconds > best[2]:
            best = (name, file_id, seconds)

    if best is None:
        return False

    # If some sibling is still rendering it might be the real lesson, so normally wait
    # rather than lock in a shorter one. Not forever: a recording that never materialises
    # must not block the one we already have.
    if rendering and now < lesson.end_datetime + timedelta(hours=PATIENCE_HOURS):
        logger.info("lesson %s: %s of %s conference(s) still rendering — waiting before claiming",
                    lesson.id, rendering, len(conference_names))
        return False

    name, file_id, seconds = best
    if len(conference_names) > 1:
        logger.info("lesson %s: %s conferences, claiming the longest (%.0fs) — %s",
                    lesson.id, len(conference_names), seconds, name)
    return bool(meet_recordings.claim_recording(db, lesson, name, file_id))


def ingest_one_pending(db, exclude: Optional[set] = None) -> bool:
    """Make one claimed recording streamable and upload it. True if one was processed.

    ``exclude`` holds ids already tried this tick; the one picked is added to it.
    """
    query = db.query(LessonRecording).filter(
        LessonRecording.status == "pending",
        LessonRecording.drive_file_id.isnot(None),
        LessonRecording.attempts < recording_ingest.MAX_ATTEMPTS,
    )
    if exclude:
        query = query.filter(~LessonRecording.id.in_(exclude))
    recording = query.order_by(LessonRecording.created_at).first()
    if recording is None:
        return False
    if exclude is not None:
        exclude.add(recording.id)

    recording.attempts += 1
    db.commit()
    try:
        recording_ingest.process_recording(db, recording)
    except Exception as e:
        db.rollback()
        recording_ingest.fail_recording(db, recording, e)
    return True


def ingest_pending(db, budget_seconds: float = INGEST_BUDGET_SECONDS, clock=time.monotonic) -> int:
    """Work through the claimed recordings until none are left or the budget is spent.

    One per tick made sense while each took ~23 minutes of re-encoding. A repackage takes
    about a minute, and up to 23 lessons end at 20:00 on a busy day: one per five-minute
    tick would leave the last of them waiting two hours for nothing. The budget only stops
    new ones from *starting*, so Meet links and polling still get their turn every few
    minutes. Each recording is tried at most once per tick — a failing one waits for the
    next tick rather than spending its three attempts back to back.
    """
    if not recording_ingest.room_on_disk():
        return 0
    started, tried, done = clock(), set(), 0
    while clock() - started < budget_seconds and ingest_one_pending(db, exclude=tried):
        done += 1
    return done


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
        summary = {"links": 0, "rooms": 0, "closed": 0, "claimed": 0, "attendance": 0, "speech": 0,
                   "ingested": 0, "transcribed": 0, "missing": 0}
        try:
            for key, fn in (
                ("links", ensure_upcoming_meet_links),
                # Talk time's switch reaches the rooms before their lessons start.
                ("rooms", meet_talk_sync.sync_rooms),
                # Before the pollers: a room left open by a student holds up both of them.
                ("closed", meet_room_closer.close_lingering_rooms),
                ("claimed", poll_for_recordings),
                ("attendance", meet_attendance.sync_if_enabled),
                # After attendance: speech is named through the people it just saved.
                ("speech", meet_talk_sync.sync_speech),
                ("ingested", ingest_pending),
                # After ingest: the words come from the recording it just made ready.
                ("transcribed", meet_talk_sync.transcribe_pending),
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
