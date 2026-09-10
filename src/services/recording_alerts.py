"""Flag lessons that ended without a recording, before payroll runs.

This is the mechanism behind "no recording, no pay" (spec §4.5). Accountants read LMS
state, never a Drive folder, so a lesson that was never recorded has to become a visible
row rather than an absence somebody notices later.

Shaped deliberately like ``MissedAttendanceLog``: same idea (a teacher was supposed to do
something and didn't), same resolvable lifecycle, so curators and accountants learn one
pattern instead of two.

Nothing here is destructive and nothing here decides pay. It raises a flag a human
resolves — a recording can still arrive late, and a lesson can legitimately have none.
"""
import logging
from datetime import datetime, timedelta, timezone

from src.schemas.models import Event, LessonRecording, MissingRecordingLog, UserInDB

logger = logging.getLogger(__name__)

# How long after a lesson ends before its absent recording counts as missing. Generous on
# purpose: Meet can take a while to finish a long recording, then we have to notice it and
# transcode it. Flagging too early trains people to ignore the flag.
GRACE_HOURS = 6

# Never look further back than this. Without it, the first run after deploy would flag
# every unrecorded lesson in the LMS's history — thousands of rows, all noise, from before
# the pipeline existed.
LOOKBACK_DAYS = 7


def sweep_missing_recordings(db) -> int:
    """Open a log row for each lesson that ended long enough ago with no ready recording.

    Idempotent: the unique constraint on ``event_id`` means re-running creates nothing new,
    so this is safe on every scheduler tick.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(hours=GRACE_HOURS)
    floor = now - timedelta(days=LOOKBACK_DAYS)

    candidates = (
        db.query(Event)
        .join(UserInDB, UserInDB.id == Event.teacher_id)
        .outerjoin(LessonRecording, LessonRecording.event_id == Event.id)
        .outerjoin(MissingRecordingLog, MissingRecordingLog.event_id == Event.id)
        .filter(
            Event.is_active.is_(True),
            Event.end_datetime < cutoff,
            Event.end_datetime > floor,
            # Only lessons this pipeline was actually responsible for. A lesson with no
            # Meet link was never ours to record, and flagging it would be a false alarm.
            Event.meeting_url.isnot(None),
            UserInDB.workspace_email.isnot(None),
            MissingRecordingLog.id.is_(None),
            LessonRecording.id.is_(None),
        )
        .limit(200)
        .all()
    )

    opened = 0
    for lesson in candidates:
        db.add(MissingRecordingLog(event_id=lesson.id, teacher_id=lesson.teacher_id))
        opened += 1
    if opened:
        db.commit()
        logger.warning("recordings: %s lesson(s) ended with no recording", opened)
    return opened


def resolve_late_arrivals(db) -> int:
    """Close flags whose recording turned up after the fact.

    Without this, a recording that arrived an hour late would leave a permanent black mark
    against a teacher who did nothing wrong.
    """
    stale = (
        db.query(MissingRecordingLog)
        .join(LessonRecording, LessonRecording.event_id == MissingRecordingLog.event_id)
        .filter(
            MissingRecordingLog.resolved_at.is_(None),
            LessonRecording.status == "ready",
        )
        .limit(200)
        .all()
    )
    for row in stale:
        row.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if stale:
        db.commit()
        logger.info("recordings: resolved %s late-arriving recording(s)", len(stale))
    return len(stale)
