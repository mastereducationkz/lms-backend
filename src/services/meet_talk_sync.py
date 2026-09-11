"""The recordings worker's talk-time steps: switch rooms' transcription, save who spoke when,
and turn recordings into words.

1. ``sync_rooms`` — Meet transcribes a call only if its room says so *before* the call starts,
   so every upcoming LMS lesson room is set to match the LMS switch. Only rooms whose state
   differs from the switch are touched (``meet_room_transcription``).
2. ``sync_speech`` — once a call has ended and its attendance is saved, Meet's transcript
   entries are reduced to who-spoke-when and kept (``meet_speech``). Meet's Russian text is
   unusable, so no text is kept at all.
3. ``transcribe_pending`` — for a lesson with speech saved and its recording ready, the audio
   goes to Deepgram once; the words come back by anonymous voice (``lesson_transcripts``).

Every Google or Deepgram call happens with no database transaction open: pgbouncer and
Postgres kill a transaction idle for 60 s.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import and_, or_, select

from src.schemas.models import (
    Event,
    EventGroup,
    Group,
    LessonRecording,
    LessonTranscript,
    MeetConference,
    MeetRoomTranscription,
    MeetSpeech,
    UserInDB,
)
from src.services import google_workspace, meet_recordings, talk_settings

logger = logging.getLogger(__name__)

# Rooms are created three days ahead (recordings_worker.SCHEDULE_HORIZON_DAYS); the switch
# reaches all of them.
ROOM_HORIZON = timedelta(days=3)
ROOMS_PER_TICK = 200

SPEECH_LOOKBACK = timedelta(hours=72)
# Meet lists a call's transcript a little after the call; with none by then, there was none.
NO_TRANSCRIPT_AFTER = timedelta(minutes=30)
# A transcript still "started" this long after its call ended is taken as it is.
STALE_TRANSCRIPT_AFTER = timedelta(hours=6)

TRANSCRIBE_BUDGET_SECONDS = 180
TRANSCRIBE_MAX_ATTEMPTS = 3
# The Drive original is kept 7 days, the Shared Drive copy longer; older lessons are not
# worth a download that may no longer exist.
TRANSCRIBE_WITHIN = timedelta(days=7)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_PARAMS = {"model": "nova-3", "language": "multi", "diarize": "true", "smart_format": "true",
                   "punctuate": "true", "utterances": "true"}

TRANSCRIPTION_MASK = "config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pages(call, key: str, **kwargs) -> list:
    items, token = [], None
    while True:
        response = call(pageSize=100, pageToken=token, **kwargs).execute()
        items.extend(response.get(key, []))
        token = response.get("nextPageToken")
        if not token:
            return items


def _naive(value: Optional[str]) -> Optional[datetime]:
    parsed = meet_recordings._rfc3339(value)
    if parsed is not None and parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _http_status(error: Exception) -> Optional[int]:
    return getattr(getattr(error, "resp", None), "status", None)


# ── 1. rooms ─────────────────────────────────────────────────────────────────────────────

def set_room_transcription(meeting_url: str, on: bool) -> None:
    """Switch Meet's automatic transcription for one lesson room. Google only."""
    code = meet_recordings.meet_code(meeting_url)
    if not code:
        raise ValueError(f"not a Meet link: {meeting_url!r}")
    meet = google_workspace.meet_client()
    space = meet.spaces().get(name=f"spaces/{code}").execute()
    meet.spaces().patch(
        name=space["name"],
        updateMask=TRANSCRIPTION_MASK,
        body={"config": {"artifactConfig": {"transcriptionConfig": {
            "autoTranscriptionGeneration": "ON" if on else "OFF"}}}},
    ).execute()


def _lms_room_clause():
    """The robot's rooms: the lesson's teacher, or a group owner, has a Workspace account."""
    teacher_has = Event.teacher_id.in_(select(UserInDB.id).where(UserInDB.workspace_email.isnot(None)))
    owner_has = Event.id.in_(
        select(EventGroup.event_id)
        .join(Group, Group.id == EventGroup.group_id)
        .join(UserInDB, UserInDB.id == Group.teacher_id)
        .where(UserInDB.workspace_email.isnot(None)))
    return or_(teacher_has, owner_has)


def rooms_to_change(db, want: bool, now: datetime, limit: int = ROOMS_PER_TICK) -> list:
    """(event id, meeting url) of upcoming LMS lesson rooms whose transcription is not ``want``."""
    query = (db.query(Event.id, Event.meeting_url)
             .outerjoin(MeetRoomTranscription, MeetRoomTranscription.event_id == Event.id)
             .filter(Event.is_active.is_(True),
                     Event.meeting_url.ilike("%meet.google.com/%"),
                     Event.end_datetime > now,
                     Event.start_datetime < now + ROOM_HORIZON,
                     _lms_room_clause()))
    if want:
        # A room never touched is off (the robot creates rooms without transcription).
        query = query.filter(or_(MeetRoomTranscription.event_id.is_(None),
                                 MeetRoomTranscription.transcription.is_(False)))
    else:
        query = query.filter(MeetRoomTranscription.transcription.is_(True))
    return query.order_by(Event.start_datetime).limit(limit).all()


def sync_rooms(db, now: Optional[datetime] = None) -> int:
    """Make every upcoming lesson room transcribe exactly when the switch says. Returns rooms changed."""
    now = now or _now()
    want = talk_settings.enabled(db)
    lessons = rooms_to_change(db, want, now)
    db.commit()
    changed = 0
    for event_id, meeting_url in lessons:
        error = None
        try:
            set_room_transcription(meeting_url, want)
            changed += 1
        except Exception as e:
            if _http_status(e) not in (403, 404) and not isinstance(e, ValueError):
                logger.warning("lesson %s: could not switch transcription %s, will retry: %s",
                               event_id, "on" if want else "off", e)
                continue  # a passing failure: try again next tick
            error = str(e)[:500]  # a room the robot cannot change: remember, do not retry
            logger.warning("lesson %s: room refuses transcription changes: %s", event_id, e)
        row = db.get(MeetRoomTranscription, event_id) or MeetRoomTranscription(event_id=event_id)
        row.transcription, row.applied_at, row.error = want, _now(), error
        db.add(row)
        db.commit()
    if changed:
        logger.info("talk time: transcription %s in %s lesson room(s)", "on" if want else "off", changed)
    return changed


# ── 2. who spoke when ────────────────────────────────────────────────────────────────────

def fetch_speech(conference_record: str) -> Optional[dict]:
    """Meet's transcript of one call as who-spoke-when, or None while it is still being written.

    Returns ``{"state": "none"}`` when the call had no transcript at all.
    """
    records = google_workspace.meet_client().conferenceRecords()
    transcripts = _pages(records.transcripts().list, "transcripts", parent=conference_record)
    if not transcripts:
        return {"state": "none"}
    if any(t.get("state") == "STARTED" for t in transcripts):
        return None
    names, index, entries, document_id = [], {}, [], None
    for transcript in transcripts:
        document_id = document_id or (transcript.get("docsDestination") or {}).get("document")
        for entry in _pages(records.transcripts().entries().list, "transcriptEntries", parent=transcript["name"]):
            who, start, end = entry.get("participant"), _naive(entry.get("startTime")), _naive(entry.get("endTime"))
            if not who or not start or not end or end <= start:
                continue
            if who not in index:
                index[who] = len(names)
                names.append(who)
            entries.append((index[who], start, end))
    if not entries:
        return {"state": "none", "document_id": document_id}
    origin = min(start for _, start, _ in entries)
    ms = lambda moment: int(round((moment - origin).total_seconds() * 1000))  # noqa: E731
    return {"state": "saved", "origin": origin, "participants": names, "document_id": document_id,
            "entries": sorted([i, ms(a), ms(b)] for i, a, b in entries)}


def sync_speech(db, now: Optional[datetime] = None) -> int:
    """Save who-spoke-when for every ended, attendance-saved call not yet read. Returns calls saved."""
    if not talk_settings.enabled(db):
        return 0
    now = now or _now()
    calls = (db.query(MeetConference.id, MeetConference.event_id, MeetConference.conference_record,
                      MeetConference.ended_at)
             .outerjoin(MeetSpeech, MeetSpeech.conference_id == MeetConference.id)
             .filter(MeetSpeech.id.is_(None),
                     MeetConference.synced_at.isnot(None),  # its people are saved: speech can be named
                     MeetConference.ended_at.isnot(None),
                     MeetConference.ended_at > now - SPEECH_LOOKBACK)
             .order_by(MeetConference.ended_at).all())
    db.commit()
    saved = 0
    for conference_id, event_id, name, ended in calls:
        try:
            speech = fetch_speech(name)
            if speech is None:
                if now - ended < STALE_TRANSCRIPT_AFTER:
                    continue
                speech = {"state": "none"}
            if speech["state"] == "none" and now - ended < NO_TRANSCRIPT_AFTER:
                continue  # Google may not have listed it yet
            db.add(MeetSpeech(conference_id=conference_id, event_id=event_id, state=speech["state"],
                              origin=speech.get("origin"), participants=speech.get("participants"),
                              entries=speech.get("entries"), document_id=speech.get("document_id")))
            db.commit()
            saved += 1
            logger.info("lesson %s: speech %s (%s entries) from %s", event_id, speech["state"],
                        len(speech.get("entries") or []), name)
        except Exception as e:
            db.rollback()
            logger.warning("talk time %s: %s", name, e)
    return saved


# ── 3. the words ─────────────────────────────────────────────────────────────────────────

def _recording_started_at(conference_record: str, drive_file_id: Optional[str]) -> Optional[datetime]:
    """When the claimed recording began — Deepgram's second 0 on the wall clock."""
    recordings = (google_workspace.meet_client().conferenceRecords().recordings()
                  .list(parent=conference_record, pageSize=10).execute().get("recordings", []))
    ours = [r for r in recordings if (r.get("driveDestination") or {}).get("file") == drive_file_id] or recordings
    starts = [_naive(r.get("startTime")) for r in ours if r.get("startTime")]
    return min(starts) if starts else None


def _audio_from_drive(file_id: str, workdir: Path) -> Path:
    from src.services import video_ingest
    from src.services.recording_ingest import _download_drive_file

    video = _download_drive_file(file_id, workdir)
    audio = workdir / "audio.ogg"
    video_ingest._run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                       "-c:a", "libopus", "-b:a", "24k", str(audio)], timeout=1800)
    video.unlink(missing_ok=True)
    return audio


def deepgram(audio: Path, key: str) -> dict:
    import httpx

    with httpx.Client(timeout=900) as client:
        response = client.post(DEEPGRAM_URL, params=DEEPGRAM_PARAMS, content=audio.read_bytes(),
                               headers={"Authorization": f"Token {key}", "Content-Type": "audio/ogg"})
    response.raise_for_status()
    return response.json()


def compact_utterances(result: dict) -> tuple:
    """Deepgram's answer → ([start, end, voice, text], {language: words}, audio seconds)."""
    utterances, languages = [], {}
    for u in (result.get("results") or {}).get("utterances") or []:
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        utterances.append([round(float(u["start"]), 2), round(float(u["end"]), 2), int(u.get("speaker") or 0), text])
        for word in u.get("words") or []:
            lang = (word.get("language") or "?")[:2]
            languages[lang] = languages.get(lang, 0) + 1
    duration = float((result.get("metadata") or {}).get("duration") or 0.0)
    return utterances, languages, duration


def lessons_to_transcribe(db, now: datetime, exclude: set) -> list:
    """Lessons with speech saved (transcription was on) and a ready recording, not transcribed yet."""
    has_speech = db.query(MeetSpeech.event_id).filter(MeetSpeech.state == "saved")
    query = (db.query(LessonRecording)
             .outerjoin(LessonTranscript, LessonTranscript.event_id == LessonRecording.event_id)
             .filter(LessonRecording.status == "ready",
                     LessonRecording.ingested_at > now - TRANSCRIBE_WITHIN,
                     or_(LessonRecording.drive_file_id.isnot(None), LessonRecording.shared_drive_file_id.isnot(None)),
                     LessonRecording.event_id.in_(has_speech),
                     or_(LessonTranscript.id.is_(None),
                         and_(LessonTranscript.status == "pending",
                              LessonTranscript.attempts < TRANSCRIBE_MAX_ATTEMPTS))))
    if exclude:
        query = query.filter(~LessonRecording.event_id.in_(exclude))
    return query.order_by(LessonRecording.ingested_at).limit(1).all()


def transcribe_one(db, recording, key: str) -> None:
    event_id = recording.event_id
    conference_record = recording.conference_record
    file_id = recording.shared_drive_file_id or recording.drive_file_id
    original = recording.drive_file_id
    row = db.query(LessonTranscript).filter_by(event_id=event_id).first()
    if row is None:
        row = LessonTranscript(event_id=event_id, status="pending", attempts=0)
        db.add(row)
    row.attempts += 1
    db.flush()
    row_id, attempt = row.id, row.attempts
    # Read everything first: touching a row after commit reloads it, which opens a transaction
    # that then sits idle through minutes of download and Deepgram — and pgbouncer kills it at
    # 60 s (lesson 15883, 2026-09-11: transcribed, then lost at the save).
    db.commit()

    # Google and Deepgram only: no transaction is open, so a failure here leaves nothing to undo.
    workdir = Path(tempfile.mkdtemp(prefix=f"talk_{event_id}_"))
    try:
        started = _recording_started_at(conference_record, original) if conference_record else None
        audio = _audio_from_drive(file_id, workdir)
        utterances, languages, duration = compact_utterances(deepgram(audio, key))
    except Exception as e:
        _save(db, row_id, error=str(e)[:1900],
              status="failed" if attempt >= TRANSCRIBE_MAX_ATTEMPTS else "pending")
        logger.warning("lesson %s: transcript failed (attempt %s): %s", event_id, attempt, str(e)[:300])
        return
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    _save(db, row_id, status="ready", error=None, recording_started_at=started, audio_seconds=duration,
          utterances=utterances, languages=languages, completed_at=_now())
    logger.info("lesson %s: transcript ready (%s utterances, %.0f min of audio)",
                event_id, len(utterances), duration / 60)


def _save(db, row_id: int, **fields) -> None:
    """Write the transcript row, once more on a fresh connection if the old one was dropped:
    a paid transcript is not lost to a closed socket."""
    from sqlalchemy.exc import OperationalError

    for attempt in (1, 2):
        try:
            row = db.get(LessonTranscript, row_id)
            for name, value in fields.items():
                setattr(row, name, value)
            db.commit()
            return
        except OperationalError as e:
            db.rollback()
            if attempt == 2:
                raise
            logger.warning("transcript %s: database connection dropped, saving again: %s",
                           row_id, str(e).splitlines()[0][:200])


def transcribe_pending(db, budget_seconds: float = TRANSCRIBE_BUDGET_SECONDS, clock=time.monotonic,
                       now: Optional[datetime] = None) -> int:
    """Transcribe lessons until none are left or the budget is spent. Returns lessons tried."""
    if not talk_settings.transcripts_enabled(db):
        return 0
    key = talk_settings.deepgram_key()
    now = now or _now()
    started, tried = clock(), set()
    while clock() - started < budget_seconds:
        found = lessons_to_transcribe(db, now, tried)
        if not found:
            break
        tried.add(found[0].event_id)
        transcribe_one(db, found[0], key)
    return len(tried)
