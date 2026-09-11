"""What a lesson's talk means: who spoke, for how long, how the lesson flowed — and its words.

``meet_talk_sync`` stores what Meet and Deepgram reported: Meet participants' speech timing,
and Deepgram's words by anonymous voice. This module reads them back as people, through the
same account links the attendance record uses (``meet_presence``), so confirming an account
names its speech — and its lines in the transcript — in every lesson at once. Nothing here is
stored.

Owner decisions, 2026-09-11: visible exactly like Meet attendance (never to students); a
"silent" student is one in the room for at least 10 minutes who never spoke — a filter for
reports, never a flag; the watch-link page gets talk time and the timeline, never the words.
"""
from __future__ import annotations

import re
import statistics
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.schemas.models import LessonRecording, LessonTranscript, MeetSpeech
from src.services import meet_presence, talk_settings
from src.services.meet_presence import LESSON_MARGIN, merge_spans
from src.utils.utc_json import utc_z

TURN_GAP = 1.5          # s: one person's speech closer than this is one turn
SILENCE_MIN = 5.0       # s: shorter gaps are breathing, not silence
WAIT_MAX = 20.0         # s: a reply later than this does not answer the question
BUCKET = 600            # s: the flow chart's resolution
STRETCH_BREAK = 30.0    # s: silence this long ends a teacher's stretch
SILENT_IN_ROOM = 10     # min in the room, never a word: "didn't speak"
MATCH_SHARE = 0.3       # a line is someone's if their speech covers this much of it
VOICE_MAJORITY = 0.6    # a voice is someone's if this much of its matched time is theirs
COVERAGE = 0.98         # without Meet's timing: a voice may be someone's only if they were in the room ~always it spoke
# Speech timing arrives a little after attendance; a lesson without it for this long had
# transcription off.
SPEECH_EXPECTED_WITHIN = timedelta(hours=7)
TRANSCRIBE_WITHIN = timedelta(days=7)

SPEAKING = ("teacher", "student", "unknown")   # whose time the shares divide
ORDER = {"teacher": 0, "student": 1, "unknown": 2, "other": 3}
QUESTION = re.compile(r"\?\s*[»\"')\]]?\s*$")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _secs(delta: timedelta) -> float:
    return delta.total_seconds()


def _clip(spans: list, lo: datetime, hi: datetime) -> list:
    return [(max(a, lo), min(b, hi)) for a, b in spans if b > lo and a < hi]


def _total(spans: list) -> float:
    return sum(_secs(b - a) for a, b in spans)


def _overlap(a: datetime, b: datetime, spans: list) -> float:
    return sum(max(0.0, _secs(min(b, y) - max(a, x))) for x, y in spans)


class _Spans:
    """One person's merged, sorted speech, asked "how much of [a, b] is theirs?" by binary search —
    a lesson is hundreds of lines against hundreds of stretches, and scanning them all for every
    line was 90% of the time (0.25 s a lesson)."""

    def __init__(self, spans: list):
        self.spans = spans
        self.starts = [a for a, _ in spans]

    def overlap(self, a: datetime, b: datetime) -> float:
        total, i = 0.0, bisect_right(self.starts, b)
        while i > 0:
            i -= 1
            x, y = self.spans[i]
            if y <= a:
                break  # merged and sorted: every earlier stretch ended earlier still
            total += _secs(min(b, y) - max(a, x))
        return total


def _turns(spans: list, gap: float = TURN_GAP) -> list:
    out = []
    for a, b in sorted(spans):
        if out and _secs(a - out[-1][1]) < gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# ── who said what, on the clock ──────────────────────────────────────────────────────────

def _speakers(event, batch, record: dict, rows: list) -> dict:
    """key → {"user_id", "name", "role", "spans"}: every stretch of speech Meet reported,
    named through the attendance record's account links."""
    by_name = {p.participant_name: p for p in batch.participants.get(event.id, [])}
    students = {s["user_id"] for s in record.get("students") or []}
    people: dict = {}
    for row in rows:
        if row.state != "saved" or not row.entries or row.origin is None:
            continue
        names = row.participants or []
        for index, start_ms, end_ms in row.entries:
            name = names[index] if 0 <= index < len(names) else None
            participant = by_name.get(name)
            if participant is None:
                key, user_id, role, label = f"x{index}:{row.id}", None, "unknown", "Someone in the call"
            else:
                user_id, hidden = batch.resolve(participant)
                shown = participant.display_name or "No name shown"
                if hidden:
                    key, user_id, role, label = f"p{participant.id}", None, "other", shown
                elif user_id is None:
                    key, role, label = f"p{participant.id}", "unknown", shown
                else:
                    user = batch.users.get(user_id)
                    key, label = f"u{user_id}", user.name if user else f"User {user_id}"
                    role = ("teacher" if user_id == event.teacher_id
                            else "student" if user_id in students else "other")
            person = people.setdefault(key, {"user_id": user_id, "name": label, "role": role, "spans": []})
            person["spans"].append((row.origin + timedelta(milliseconds=start_ms),
                                    row.origin + timedelta(milliseconds=end_ms)))
    for person in people.values():
        person["spans"] = merge_spans(person["spans"])
    return people


def _moment(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None) if value else None


def _present(person: dict) -> list:
    return merge_spans([(_moment(s["joined_at"]), _moment(s["left_at"]))
                        for s in person.get("sessions") or [] if s.get("joined_at") and s.get("left_at")])


def _voice_speakers(record: dict, transcript) -> dict:
    """The same people dict as ``_speakers``, for a lesson Meet was not transcribing (talk time
    was off): each of Deepgram's voices is a person only where who was in the room leaves one
    choice — the teacher for the most talkative voice they could be, a student for a voice only
    they were in the room for. Every other voice stays «Голос N», counted with the students."""
    started = transcript.recording_started_at
    voices: dict = {}
    for a, b, voice, _text in transcript.utterances or []:
        voices.setdefault(voice, []).append((started + timedelta(seconds=a), started + timedelta(seconds=b)))
    seconds = {v: _total(spans) for v, spans in voices.items()}

    def could_be(person: dict, spans: list) -> bool:
        present = _present(person)
        return sum(_overlap(a, b, present) for a, b in spans) >= COVERAGE * max(0.1, _total(spans))

    owner = {}
    teacher = record.get("teacher")
    if teacher:
        for voice in sorted(voices, key=lambda v: -seconds[v]):
            if could_be(teacher, voices[voice]):
                owner[voice] = ("teacher", teacher)
                break
    for voice, spans in voices.items():
        if voice not in owner:
            fits = [s for s in record.get("students") or [] if could_be(s, spans)]
            if len(fits) == 1:
                owner[voice] = ("student", fits[0])

    people: dict = {}
    for voice, spans in voices.items():
        if voice in owner:
            role, person = owner[voice]
            entry = people.setdefault(f"u{person['user_id']}", {"user_id": person["user_id"], "name": person["name"],
                                                               "role": role, "spans": []})
        else:
            entry = people.setdefault(f"v{voice}", {"user_id": None, "name": f"Голос {voice + 1}",
                                                    "role": "unknown", "spans": []})
        entry["spans"].extend(spans)
    for person in people.values():
        person["spans"] = merge_spans(person["spans"])
    return people


def _name_lines(utterances: list, started: Optional[datetime], people: dict) -> list:
    """Deepgram's utterances as turns, each named by Meet's timing where it can be."""
    if not utterances:
        return []
    placed = []
    by_voice: dict = {}
    index = {key: _Spans(person["spans"]) for key, person in people.items() if person["spans"]}
    for start, end, voice, text in utterances:
        best, best_overlap = None, 0.0
        if started is not None:
            a, b = started + timedelta(seconds=start), started + timedelta(seconds=end)
            for key, spans in index.items():
                o = spans.overlap(a, b)
                if o > best_overlap:
                    best, best_overlap = key, o
            if best and best_overlap >= MATCH_SHARE * max(0.1, end - start):
                by_voice.setdefault(voice, {}).setdefault(best, 0.0)
                by_voice[voice][best] += best_overlap
            else:
                best = None
        placed.append([start, end, voice, text, best])

    majority = {}
    for voice, owners in by_voice.items():
        key, seconds = max(owners.items(), key=lambda kv: kv[1])
        if seconds >= VOICE_MAJORITY * sum(owners.values()):
            majority[voice] = key

    lines: list = []
    for start, end, voice, text, key in placed:
        key = key or majority.get(voice)
        who = key or f"voice:{voice}"
        if lines and lines[-1]["_who"] == who and start - lines[-1]["end"] < TURN_GAP:
            lines[-1]["end"], lines[-1]["text"] = end, f'{lines[-1]["text"]} {text}'
            continue
        person = people.get(key) if key else None
        lines.append({"_who": who, "at": start, "end": end, "speaker_key": key,
                      "speaker_label": person["name"] if person else f"Голос {voice + 1}",
                      "role": person["role"] if person else None, "text": text})
    return lines


def _insights(lines: list) -> tuple:
    """The class's interaction, and who answered the teacher's questions: (insights, answers by key).

    A teacher's line ending in «?» is a question put to the room; the first other voice within
    20 s answered it — that person is credited with the answer.
    """
    questions, answered, waits, answers = 0, 0, [], {}
    for i, line in enumerate(lines):
        if line["role"] != "teacher" or not QUESTION.search(line["text"]):
            continue
        questions += 1
        reply = next((n for n in lines[i + 1:] if n["role"] != "teacher"), None)
        if reply and reply["role"] != "other" and reply["at"] - line["end"] <= WAIT_MAX:
            answered += 1
            waits.append(max(0.0, reply["at"] - line["end"]))
            if reply["speaker_key"]:
                answers[reply["speaker_key"]] = answers.get(reply["speaker_key"], 0) + 1
    return {
        "teacher_questions": questions,
        "answered": answered,
        "median_wait_seconds": round(statistics.median(waits), 1) if waits else None,
        "student_questions": sum(1 for n in lines if n["role"] in ("student", "unknown") and QUESTION.search(n["text"])),
    }, answers


# ── one lesson ───────────────────────────────────────────────────────────────────────────

def _recording_start(event, batch, recording) -> Optional[datetime]:
    """When the claimed recording's call began (Meet starts recording as the call opens)."""
    if recording is None:
        return None
    calls = batch.conferences.get(event.id, [])
    match = next((c for c in calls if c.conference_record == recording.conference_record), None)
    if match is None:
        match = max((c for c in calls if c.started_at), key=lambda c: (c.ended_at or c.started_at) - c.started_at,
                    default=None)
    return match.started_at if match else None


def _has_words(transcript) -> bool:
    """A transcript that can stand in for Meet's timing: ready, and placed on the clock."""
    return bool(transcript is not None and transcript.status == "ready" and transcript.recording_started_at
                and transcript.utterances)


def has_talk(rows: list, transcript) -> bool:
    return any(r.state == "saved" for r in rows) or _has_words(transcript)


def compute(event, batch, record: dict, rows: list, *, transcript=None, recording=None,
            with_transcript: bool = False, viewer_role: Optional[str] = None, now: Optional[datetime] = None,
            transcripts_on: bool = True) -> dict:
    """The talk of one lesson whose attendance record is ready and whose speech is saved."""
    now = now or _now()
    start, end = event.start_datetime, event.end_datetime or event.start_datetime + timedelta(hours=1)
    lo, hi = start - LESSON_MARGIN, end + LESSON_MARGIN
    # Meet's speaker timing when the lesson had it; otherwise the transcript's voices.
    source = "meet" if any(r.state == "saved" for r in rows) else "voices"
    people = (_speakers(event, batch, record, rows) if source == "meet"
              else _voice_speakers(record, transcript) if _has_words(transcript) else {})

    in_room = {}
    teacher = record.get("teacher")
    if teacher:
        in_room[f"u{teacher['user_id']}"] = (bool(teacher.get("sessions")), teacher.get("minutes_in_lesson", 0))
        people.setdefault(f"u{teacher['user_id']}", {"user_id": teacher["user_id"], "name": teacher["name"],
                                                     "role": "teacher", "spans": []})
    for s in record.get("students") or []:
        in_room[f"u{s['user_id']}"] = (bool(s.get("sessions")), s.get("minutes_in_lesson", 0))
        people.setdefault(f"u{s['user_id']}", {"user_id": s["user_id"], "name": s["name"],
                                               "role": "student", "spans": []})

    lines, started = [], None
    if transcript is not None and transcript.status == "ready":
        started = transcript.recording_started_at
        lines = _name_lines(transcript.utterances or [], started, people)
    # Where the lesson starts inside the recording: video second = lesson second + this. Known
    # even without a transcript — the call the recording came from began at a known moment — so
    # the watch page can follow the video too.
    anchor = started or _recording_start(event, batch, recording)
    offset = _secs(start - anchor) if anchor else None

    insights, answers = _insights(lines) if lines else (None, {})
    rows_out = []
    for key, person in people.items():
        spans = _clip(person["spans"], lo, hi)
        turns = _turns(spans)
        present = in_room.get(key, (True, None))[0]
        rows_out.append({
            "key": key, "user_id": person["user_id"], "name": person["name"], "role": person["role"],
            "seconds": round(_total(spans)), "turns": len(turns),
            "longest_turn_seconds": round(max((_secs(b - a) for a, b in turns), default=0.0)),
            "in_room": present,
            "questions": (sum(1 for n in lines if n["speaker_key"] == key and QUESTION.search(n["text"]))
                          if lines else None),
            # Teacher questions this person answered first; None without a transcript.
            "answers": answers.get(key, 0) if lines else None,
            "spans": [[round(_secs(a - start), 1), round(_secs(b - start), 1)] for a, b in spans],
        })
    speaking_total = sum(r["seconds"] for r in rows_out if r["role"] in SPEAKING)
    for r in rows_out:
        r["share"] = round(r["seconds"] / speaking_total, 3) if speaking_total and r["role"] in SPEAKING else 0.0
    rows_out.sort(key=lambda r: (ORDER[r["role"]], -r["seconds"], r["name"].lower()))

    teacher_seconds = sum(r["seconds"] for r in rows_out if r["role"] == "teacher")
    student_seconds = sum(r["seconds"] for r in rows_out if r["role"] in ("student", "unknown"))

    # The scheduled lesson: silence, flow, stretches.
    everyone = merge_spans([s for p in people.values() for s in _clip(p["spans"], start, end)])
    silence, cursor = 0.0, start
    for a, b in everyone + [(end, end)]:
        if _secs(a - cursor) >= SILENCE_MIN:
            silence += _secs(a - cursor)
        cursor = max(cursor, b)
    lesson_seconds = max(1.0, _secs(end - start))

    buckets = []
    for k in range(int(lesson_seconds // BUCKET) + (1 if lesson_seconds % BUCKET else 0)):
        b0 = start + timedelta(seconds=k * BUCKET)
        b1 = min(end, b0 + timedelta(seconds=BUCKET))
        of = lambda roles: round(sum(_total(_clip(p["spans"], b0, b1)) for p in people.values()  # noqa: E731
                                     if p["role"] in roles))
        buckets.append({"from_minute": k * BUCKET // 60, "teacher_seconds": of(("teacher",)),
                        "students_seconds": of(("student", "unknown"))})

    turns_all = sorted((a, b, key, people[key]["role"]) for key in people
                       for a, b in _turns(_clip(people[key]["spans"], start, end)))
    changes = sum(1 for x, y in zip(turns_all, turns_all[1:]) if x[2] != y[2])
    longest, run_start, run_end = 0.0, None, None
    for a, b, _key, role in turns_all:
        if role == "teacher":
            if run_start is None or _secs(a - run_end) > STRETCH_BREAK:
                run_start = a
            run_end = max(run_end or b, b)
            longest = max(longest, _secs(run_end - run_start))
        else:
            run_start = run_end = None

    held_back = any(r["role"] == "unknown" and r["seconds"] > 0 for r in rows_out)
    silent = [{"user_id": r["user_id"], "name": r["name"]} for r in rows_out
              if r["role"] == "student" and r["seconds"] == 0
              and (in_room.get(r["key"], (False, 0))[1] or 0) >= SILENT_IN_ROOM]
    if source == "voices" and held_back:
        silent = []  # an unnamed voice may be any of them

    out = {
        "state": "ready",
        "source": source,
        "lesson_seconds": round(lesson_seconds),
        "speech_seconds": round(_total(everyone)),
        "silence_seconds": round(silence),
        "teacher_share": round(teacher_seconds / speaking_total, 3) if speaking_total else None,
        "students_share": round(student_seconds / speaking_total, 3) if speaking_total else None,
        "teacher_seconds": teacher_seconds,
        "people": rows_out,
        "silent_students": silent,
        "buckets": buckets,
        "longest_teacher_stretch_seconds": round(longest),
        "speaker_changes_per_10_min": round(changes / (lesson_seconds / 600), 1),
        "held_back": held_back,
        "recording_offset_seconds": round(offset, 2) if offset is not None else None,
        "insights": insights,
    }
    if with_transcript:
        out["transcript"] = transcript_block(event, recording, transcript, lines, started,
                                             viewer_role=viewer_role, now=now, transcripts_on=transcripts_on)
    return out


def transcript_block(event, recording, transcript, lines: list, started, *, viewer_role: Optional[str],
                     now: datetime, transcripts_on: bool) -> dict:
    """The transcript part of a lesson's talk, in whatever state it is."""
    offset = _secs(event.start_datetime - started) if started else None
    block = {"state": "not_available", "error": None, "recording_offset_seconds": offset, "lines": []}
    if transcript is not None and transcript.status == "ready":
        block["state"] = "ready"
        block["lines"] = [{"at": round(n["at"], 2), "end": round(n["end"], 2),
                           "lesson_at": round(n["at"] - offset, 2) if offset is not None else round(n["at"], 2),
                           "speaker_key": n["speaker_key"], "speaker_label": n["speaker_label"],
                           "role": n["role"], "text": n["text"]} for n in lines]
        return block
    if transcript is not None and transcript.status == "failed":
        block["state"] = "failed"
        if viewer_role == "admin":
            block["error"] = (transcript.error or "")[:300] or None
        return block
    if transcript is not None:  # pending: queued or retrying
        block["state"] = "pending" if transcripts_on else "off"
        return block
    if recording is None:
        return block
    if recording.ingested_at is not None and recording.ingested_at < now - TRANSCRIBE_WITHIN:
        return block
    block["state"] = "pending" if transcripts_on else "off"
    return block


def _state_without_speech(event, batch, rows: list, now: datetime) -> str:
    synced = [c for c in batch.conferences.get(event.id, []) if c.synced_at]
    read = {r.conference_id for r in rows}
    end = event.end_datetime or event.start_datetime
    if any(c.id not in read for c in synced) and now < end + SPEECH_EXPECTED_WITHIN:
        return "waiting"
    return "none"


def lesson_talk(db, event, *, viewer_role: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """GET /meet-attendance/lessons/{id}/talk: everything the lesson's talk panel shows."""
    now = now or _now()
    base = {"event_id": event.id, "title": event.title, "start": utc_z(event.start_datetime),
            "end": utc_z(event.end_datetime)}
    settings = talk_settings.current(db)
    if not settings["enabled"]:
        return {**base, "state": "off", "enabled": False}
    (record,), batch = meet_presence.records_with_batch(db, [event], now)
    base["enabled"] = True
    if record["state"] != "ready":
        return {**base, "state": record["state"]}
    rows = db.query(MeetSpeech).filter(MeetSpeech.event_id == event.id).all()
    transcript = db.query(LessonTranscript).filter(LessonTranscript.event_id == event.id).first()
    if not has_talk(rows, transcript):
        return {**base, "state": _state_without_speech(event, batch, rows, now)}
    recording = db.query(LessonRecording).filter(LessonRecording.event_id == event.id).first()
    transcripts_on = bool(settings["transcripts"] and talk_settings.deepgram_key())
    return {**base, **compute(event, batch, record, rows, transcript=transcript, recording=recording,
                              with_transcript=True, viewer_role=viewer_role, now=now,
                              transcripts_on=transcripts_on)}


# ── many lessons ─────────────────────────────────────────────────────────────────────────

def speech_by_event(db, event_ids: list) -> dict:
    rows: dict = {}
    for row in db.query(MeetSpeech).filter(MeetSpeech.event_id.in_(event_ids or [-1])):
        rows.setdefault(row.event_id, []).append(row)
    return rows


def transcripts_by_event(db, event_ids: list) -> dict:
    return {t.event_id: t for t in db.query(LessonTranscript)
            .filter(LessonTranscript.event_id.in_(event_ids or [-1]), LessonTranscript.status == "ready")}


def summary(talk: dict) -> dict:
    """The few numbers a list row and the journal need."""
    return {
        "teacher_share": talk["teacher_share"],
        "students_share": talk["students_share"],
        "speech_seconds": talk["speech_seconds"],
        "teacher_seconds": talk["teacher_seconds"],
        "silent": [s["user_id"] for s in talk["silent_students"]],
        "seconds": {str(p["user_id"]): p["seconds"] for p in talk["people"] if p["role"] == "student"},
    }


def summaries(db, events: list, records: list, batch, now: Optional[datetime] = None) -> dict:
    """event id → the list row's talk numbers, or None (no speech saved, or not ready)."""
    now = now or _now()
    ready = {r["event_id"]: r for r in records if r.get("state") == "ready"}
    speech = speech_by_event(db, list(ready))
    # Words only where there is no Meet timing: then they are what names the voices.
    wordy = transcripts_by_event(db, [eid for eid in ready if not any(r.state == "saved" for r in speech.get(eid, []))])
    out = {}
    for event in events:
        rows = speech.get(event.id) or []
        if event.id not in ready or not has_talk(rows, wordy.get(event.id)):
            out[event.id] = None
            continue
        out[event.id] = summary(compute(event, batch, ready[event.id], rows, transcript=wordy.get(event.id), now=now))
    return out


def public_talk(talk: dict) -> Optional[dict]:
    """What the watch-link page may show: names, minutes, shares and the timeline — no words,
    no ids (owner, 2026-09-11)."""
    if not talk or talk.get("state") != "ready":
        return None
    return {
        "state": "ready",
        "source": talk.get("source", "meet"),
        "lesson_seconds": talk["lesson_seconds"],
        # The video's clock against the lesson's, so the page can follow playback. Nothing private.
        "recording_offset_seconds": talk.get("recording_offset_seconds"),
        "speech_seconds": talk["speech_seconds"],
        "silence_seconds": talk["silence_seconds"],
        "teacher_share": talk["teacher_share"],
        "students_share": talk["students_share"],
        "people": [{"name": p["name"], "role": p["role"], "seconds": p["seconds"], "share": p["share"],
                    "in_room": p["in_room"], "spans": p["spans"]} for p in talk["people"]],
        "silent_students": [s["name"] for s in talk["silent_students"]],
        "buckets": talk["buckets"],
    }
