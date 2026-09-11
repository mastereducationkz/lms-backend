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

import argparse
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

# The words: Whisper hears Russian better than Deepgram, and the audio it is given is only the
# speech — Meet says when that is — so it never hallucinates through a silent test. Deepgram stays
# for lessons Meet was not transcribing: there, its voices are the only way to tell people apart.
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
WHISPER_WORKERS = 4
CHUNK_MAX_SECONDS = 110      # one request; long enough to give Whisper context
CHUNK_JOIN_GAP = 3.0         # silence longer than this is cut out
CHUNK_PAD = 0.4              # a moment either side so no word is clipped
GLOSSARY = ("Урок SAT: русская речь с английскими терминами (main idea, inference, evidence, "
            "transition, passage).")
# Whisper's stock phrases when it is given near-silence; none of them belong in a lesson.
HALLUCINATIONS = ("продолжение следует", "субтитры делал", "субтитры создавал", "редактор субтитров",
                  "спасибо за внимание", "ставьте лайк", "подписывайтесь на канал", "thanks for watching",
                  "amara.org", "dimatorzok")

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


def speech_chunks(regions: list, *, gap: float = CHUNK_JOIN_GAP, longest: float = CHUNK_MAX_SECONDS) -> list:
    """Stretches of speech grouped into pieces to transcribe: short gaps kept, long silence cut."""
    chunks = []
    for start, end in sorted(regions):
        if chunks and start - chunks[-1][1] < gap and end - chunks[-1][0] < longest:
            chunks[-1][1] = max(chunks[-1][1], end)
        else:
            chunks.append([start, max(end, start)])
    out = []
    for start, end in chunks:
        while end - start > longest:   # one long stretch of talking, cut into sendable pieces
            out.append((start, start + longest))
            start += longest
        if end > start:
            out.append((start, end))
    return out


def speech_intervals(db, event_id: int) -> list:
    """When anyone was speaking, as moments — Meet's entries, echo and all (it is the same audio
    either way; who said it is worked out when the lesson is read)."""
    out = []
    for row in db.query(MeetSpeech).filter(MeetSpeech.event_id == event_id, MeetSpeech.state == "saved"):
        if not row.entries or row.origin is None:
            continue
        for _index, a_ms, b_ms in row.entries:
            out.append((row.origin + timedelta(milliseconds=a_ms), row.origin + timedelta(milliseconds=b_ms)))
    return sorted(out)


def regions_in_recording(speech: list, started: datetime) -> list:
    """Those moments as seconds from the recording's first frame."""
    seconds = [((a - started).total_seconds(), (b - started).total_seconds()) for a, b in speech]
    return [(max(0.0, a), b) for a, b in seconds if b > 0]


def _with_punctuation(text: str, words: list) -> list:
    """Whisper's word times carry no punctuation; its text does. Walk one along the other."""
    out, cursor = [], 0
    for word in words:
        plain = (word.get("word") or "").strip()
        if not plain:
            continue
        at = text.lower().find(plain.lower(), cursor)
        if at == -1:
            out.append((word["start"], word["end"], plain))
            continue
        end = at + len(plain)
        while end < len(text) and text[end] in ".,!?…:;»\"')":
            end += 1
        out.append((word["start"], word["end"], text[at:end].strip()))
        cursor = end
    return out


def _looks_hallucinated(text: str) -> bool:
    low = text.lower()
    if any(phrase in low for phrase in HALLUCINATIONS):
        return True
    parts = [p.strip() for p in low.replace("!", ".").replace("?", ".").split(".") if p.strip()]
    return len(parts) >= 4 and len(set(parts)) == 1   # the same sentence over and over


def whisper_chunk(audio: Path, start: float, end: float, key: str, context: str, workdir: Path) -> tuple:
    """One piece of audio → [(start, end, word)] on the recording's clock, and its text."""
    import httpx

    clip = workdir / f"chunk_{int(start)}.ogg"
    from src.services import video_ingest

    video_ingest._run(["ffmpeg", "-y", "-v", "error", "-ss", str(max(0.0, start - CHUNK_PAD)),
                       "-t", str(end - start + 2 * CHUNK_PAD), "-i", str(audio), "-c", "copy", str(clip)],
                      timeout=120)
    try:
        with httpx.Client(timeout=300) as client:
            response = client.post(
                WHISPER_URL, headers={"Authorization": f"Bearer {key}"},
                data={"model": WHISPER_MODEL, "response_format": "verbose_json",
                      "timestamp_granularities[]": "word", "prompt": (GLOSSARY + " " + context)[-400:]},
                files={"file": (clip.name, clip.read_bytes(), "audio/ogg")})
        response.raise_for_status()
        body = response.json()
    finally:
        clip.unlink(missing_ok=True)
    text = (body.get("text") or "").strip()
    if not text or _looks_hallucinated(text):
        return [], ""
    base = max(0.0, start - CHUNK_PAD)
    words = [(round(base + a, 2), round(base + b, 2), w)
             for a, b, w in _with_punctuation(text, body.get("words") or [])]
    return words, text


def whisper_words(audio: Path, chunks: list, key: str, workdir: Path) -> tuple:
    """Every chunk through Whisper, in parallel, in order. Returns (words, languages, seconds)."""
    from concurrent.futures import ThreadPoolExecutor

    # Context comes from the piece before, so the pieces are done in order within each worker's turn.
    results: list = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=WHISPER_WORKERS) as pool:
        futures = {pool.submit(whisper_chunk, audio, a, b, key, "", workdir): i
                   for i, (a, b) in enumerate(chunks)}
        for future in futures:
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as e:
                logger.warning("whisper chunk %s failed: %s", index, str(e)[:200])
                results[index] = ([], "")
    words = [w for chunk in results if chunk for w in chunk[0]]
    seconds = sum(b - a for a, b in chunks)
    return words, {"ru": sum(1 for _s, _e, w in words if any("а" <= c <= "я" for c in w.lower()))}, seconds


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


def transcribe_one(db, recording, key: Optional[str], openai_key: Optional[str] = None) -> None:
    """One lesson's words. With Meet's speaker timing, only the speech is sent to Whisper; without
    it (a lesson from before talk time), Deepgram's voices are the only way to tell people apart."""
    event_id = recording.event_id
    conference_record = recording.conference_record
    file_id = recording.shared_drive_file_id or recording.drive_file_id
    original = recording.drive_file_id
    speech = speech_intervals(db, event_id)
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
        words, utterances, provider = [], [], "deepgram"
        if openai_key and speech and started:
            chunks = speech_chunks(regions_in_recording(speech, started))
            words, languages, duration = whisper_words(audio, chunks, openai_key, workdir)
            provider = "whisper"
        if not words:
            if not key:
                raise RuntimeError("no words came back and there is no Deepgram key to fall back on")
            utterances, languages, duration = compact_utterances(deepgram(audio, key))
            provider = "deepgram"
    except Exception as e:
        _save(db, row_id, error=str(e)[:1900],
              status="failed" if attempt >= TRANSCRIBE_MAX_ATTEMPTS else "pending")
        logger.warning("lesson %s: transcript failed (attempt %s): %s", event_id, attempt, str(e)[:300])
        return
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    _save(db, row_id, status="ready", error=None, recording_started_at=started, audio_seconds=duration,
          utterances=utterances, words=words, languages=languages, provider=provider, completed_at=_now())
    logger.info("lesson %s: transcript ready by %s (%s words, %s utterances, %.0f min of audio)",
                event_id, provider, len(words), len(utterances), duration / 60)


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
    key, openai_key = talk_settings.deepgram_key(), talk_settings.openai_key()
    now = now or _now()
    started, tried = clock(), set()
    while clock() - started < budget_seconds:
        found = lessons_to_transcribe(db, now, tried)
        if not found:
            break
        tried.add(found[0].event_id)
        transcribe_one(db, found[0], key, openai_key)
    return len(tried)


def transcribe_lessons(db, event_ids: list, key: Optional[str], again: bool = False) -> dict:
    """Transcribe chosen lessons now, speech timing or not — for lessons taught before talk time
    was on (owner, 2026-09-11). Their voices are then named from who was in the room. A lesson
    already transcribed is left alone unless ``again`` says to do it over (a better model)."""
    out = {}
    for event_id in event_ids:
        recording = (db.query(LessonRecording)
                     .filter(LessonRecording.event_id == event_id, LessonRecording.status == "ready",
                             or_(LessonRecording.drive_file_id.isnot(None),
                                 LessonRecording.shared_drive_file_id.isnot(None)))
                     .first())
        if recording is None:
            out[event_id] = "no ready recording"
            continue
        row = db.query(LessonTranscript).filter_by(event_id=event_id).first()
        if row is not None and row.status == "ready" and not again:
            out[event_id] = "already transcribed"
            continue
        if row is not None:
            row.attempts, row.status = 0, "pending"
            db.commit()
            recording = db.query(LessonRecording).filter_by(event_id=event_id).first()
        transcribe_one(db, recording, key, talk_settings.openai_key())
        row = db.query(LessonTranscript).filter_by(event_id=event_id).first()
        out[event_id] = row.status if row.status == "ready" else f"{row.status}: {(row.error or '')[:200]}"
        db.commit()
    return out


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="Transcribe chosen lessons now (e.g. from before talk time was on).")
    parser.add_argument("event_ids", type=int, nargs="+")
    parser.add_argument("--again", action="store_true", help="do a finished transcript over")
    args = parser.parse_args(argv)
    key = talk_settings.deepgram_key()
    if not key and not talk_settings.openai_key():
        raise SystemExit("neither DEEPGRAM_API_KEY nor OPENAI_API_KEY is set")
    from src.config import SessionLocal

    db = SessionLocal()
    try:
        for event_id, result in transcribe_lessons(db, args.event_ids, key, again=args.again).items():
            print(f"lesson {event_id}: {result}")
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
