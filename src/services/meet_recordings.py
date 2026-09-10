"""Find finished Meet recordings and bind each to the lesson it came from.

**Polling, not Pub/Sub.** Spec §4.3 originally described a Workspace Events subscription
pushing ``fileGenerated`` to Pub/Sub. Pub/Sub requires a billing account, whose country is
fixed at creation and cannot be changed afterwards — and this tenant's Cloud profile reads
Sweden while the company is Kazakhstan. Polling ``conferenceRecords.list`` reaches the
same place with the same explicit join, needs no billing account at all, and its only cost
is latency bounded by the poll interval. Lessons end on a timetable; ingest within the
hour is fine. Owner decision, 2026-09-10.

**The join is explicit.** Meet tells us a conference happened in some *space*; we ask the
space for its ``meetingUri`` and match that against ``Event.meeting_url``, which we wrote
when we created the lesson's calendar event. Nothing is inferred from timestamps or
filenames — back-to-back lessons and reschedules make that guesswork (§4.3).
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

# via the re-export shim, not src.events.models directly: importing that module first
# triggers a circular import through src/models/__init__.py. This is the convention the
# rest of src/services/ follows.
from src.schemas.models import Event, LessonRecording
from src.services import google_workspace

logger = logging.getLogger(__name__)

# How far back each poll looks. Comfortably longer than the poll interval so a tick that
# is skipped, slow, or lands during a deploy does not create a hole; the (event_id)
# uniqueness constraint makes re-seeing the same conference harmless.
LOOKBACK_HOURS = 24

# Meet links look like https://meet.google.com/abc-defg-hij (sometimes with query params).
_MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", re.I)


class RecordingNotReady(Exception):
    """The conference exists but its recording is still being produced.

    Explicitly *not* an error: Meet publishes the conference record before the file is
    finished, so this is the normal state for a lesson that has just ended. The caller
    leaves the row pending and looks again next tick.
    """


class NoRecording(Exception):
    """The conference ended without recording anything, and never will.

    Auto-recording starts only when someone from the organisation is in the call, so a room
    opened by students alone — or a quick test call — closes with no recording at all. Meet
    creates the recording entry the moment recording *starts*, and only ended conferences are
    asked about, so an empty list is final. It used to be read as "not ready yet", and the
    worker waited out its whole patience window for a file that could not exist: the first
    live lesson (14156, 2026-09-10) sat unclaimed behind two morning test calls.

    Deliberately not a RecordingNotReady, so no caller can mistake one for the other.
    """


def meet_code(url: Optional[str]) -> Optional[str]:
    """The stable part of a Meet URL, lowercased — what we actually match on.

    Comparing whole URLs is fragile: Calendar hands back
    ``https://meet.google.com/abc-defg-hij`` while the Meet API's ``meetingUri`` may carry
    query parameters or a different prefix. The meeting code is the invariant.
    """
    if not url:
        return None
    m = _MEET_CODE_RE.search(url)
    return m.group(1).lower() if m else None


def list_recent_conferences(lookback_hours: int = LOOKBACK_HOURS) -> list:
    """Conferences that ended inside the lookback window."""
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    client = google_workspace.meet_client()
    out, page_token = [], None
    while True:
        resp = client.conferenceRecords().list(
            pageSize=100,
            filter=f'end_time>="{since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}"',
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("conferenceRecords", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return out


def _rfc3339(value: Optional[str]) -> Optional[datetime]:
    """Parse Meet's timestamps, which carry a Z and more than 6 fractional digits."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        offset = tail[len(tail) - 6:] if "+" in tail or "-" in tail else "+00:00"
        text = f"{head}.{digits:0<6}{offset}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def resolve_recording_detail(conference_record_name: str) -> tuple:
    """Conference record → ``(drive_file_id, duration_seconds)``.

    The duration matters because a Meet *space* accumulates one conference per
    join-to-empty cycle, and with auto-recording every one of them produces a file. A
    student who joins fifteen minutes early and leaves creates a complete, finished,
    perfectly valid recording of an empty room — and it lands in Drive *before* the real
    lesson has even started. Duration is how we tell the lesson from the noise.

    Raises RecordingNotReady when the conference has a recording whose file has not landed
    — an ordinary "come back later". Raises NoRecording when it never recorded at all, which
    is final.
    """
    client = google_workspace.meet_client()
    resp = client.conferenceRecords().recordings().list(
        parent=conference_record_name, pageSize=10
    ).execute()

    recordings = resp.get("recordings", [])
    if not recordings:
        raise NoRecording(f"{conference_record_name}: ended without recording")

    for rec in recordings:
        file_id = (rec.get("driveDestination") or {}).get("file")
        if not file_id:
            continue
        start, end = _rfc3339(rec.get("startTime")), _rfc3339(rec.get("endTime"))
        seconds = (end - start).total_seconds() if start and end else 0.0
        return file_id, seconds
    raise RecordingNotReady(f"{conference_record_name}: recording still processing")


def resolve_recording(conference_record_name: str) -> str:
    """Conference record → the Drive file id Meet produced."""
    return resolve_recording_detail(conference_record_name)[0]


def space_meet_code(space_name: str) -> Optional[str]:
    """The meeting code for a Meet space, or None if we cannot read it.

    Returning None rather than raising is deliberate: a space we cannot read is a
    conference that is not ours (someone's ad-hoc meeting), and the poller should skip it
    quietly rather than treat every stranger's meeting as an incident.
    """
    try:
        space = google_workspace.meet_client().spaces().get(name=space_name).execute()
    except Exception as e:
        logger.debug("space %s unreadable (probably not ours): %s", space_name, e)
        return None
    return meet_code(space.get("meetingUri"))


def match_lesson(db, code: Optional[str]) -> Optional[Event]:
    """The lesson whose Meet link carries this meeting code."""
    if not code:
        return None
    return (
        db.query(Event)
        .filter(Event.meeting_url.ilike(f"%{code}%"))
        .order_by(Event.start_datetime.desc())
        .first()
    )


def copy_to_shared_drive(drive_file_id: str, event: Event) -> str:
    """Copy the recording into the lesson Shared Drive; return the new file id.

    **Copy, never move.** Moving a file out of Meet's own recordings folder is reported to
    revert, which would silently undo the archive (§4.3 step 5). The original stays where
    Meet put it and is deleted on the 7-day retention schedule instead.

    The destination is ``Teacher / Group /`` inside the Shared Drive, and the teacher can
    read their own folder — see ``recording_archive``.
    """
    from src.services import recording_archive

    drive = google_workspace.drive_client()
    created = drive.files().copy(
        fileId=drive_file_id,
        body={
            "name": recording_archive.lesson_file_name(event),
            "parents": [recording_archive.ensure_lesson_folder(event)],
        },
        supportsAllDrives=True,
        fields="id",
    ).execute()
    return created["id"]


def claim_recording(db, event: Event, conference_record: str, drive_file_id: str) -> Optional[LessonRecording]:
    """Create (or update) the lesson's recording row, exactly once.

    Polling is at-least-once — the same conference reappears on every tick until it
    reaches a terminal state — so this is the choke point that turns repeated sightings
    into one row. Returns None when the lesson already has a recording claimed, which is
    the common case and not worth logging as anything.
    """
    existing = db.query(LessonRecording).filter(LessonRecording.event_id == event.id).first()
    if existing:
        if existing.drive_file_id is None:
            existing.drive_file_id = drive_file_id
            existing.conference_record = conference_record
            db.commit()
        return None

    row = LessonRecording(
        event_id=event.id,
        conference_record=conference_record,
        drive_file_id=drive_file_id,
        status="pending",
    )
    db.add(row)
    db.commit()
    logger.info("lesson %s: claimed recording %s", event.id, drive_file_id)
    return row
