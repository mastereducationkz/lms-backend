"""The worker's talk-time steps: rooms follow the switch, speech is saved, recordings become words.

Google and Deepgram are faked; lessons, calls and rows are real.
"""
from datetime import datetime, timedelta

import pytest

from src.schemas.models import (
    LessonRecording,
    LessonTranscript,
    MeetConference,
    MeetRoomTranscription,
    MeetSpeech,
)
from src.services import google_workspace, meet_talk_sync, talk_settings
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


class _Call:
    def __init__(self, body=None, error=None):
        self.body, self.error = body or {}, error

    def execute(self):
        if self.error:
            raise self.error
        return self.body


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()


class FakeMeet:
    """spaces.get/patch, and conferenceRecords.transcripts(.entries).list."""

    def __init__(self):
        self.patched = []          # (space, ON/OFF)
        self.refuse = {}           # code -> exception
        self.transcript_lists = {}  # conference record -> [transcript]
        self.entries = {}          # transcript name -> [entry]

    # spaces
    def spaces(self):
        return self

    def get(self, name):
        code = name.split("/", 1)[1]
        if code in self.refuse:
            return _Call(error=self.refuse[code])
        return _Call({"name": f"spaces/id-{code}"})

    def patch(self, name, updateMask, body):
        assert updateMask == meet_talk_sync.TRANSCRIPTION_MASK
        self.patched.append((name, body["config"]["artifactConfig"]["transcriptionConfig"]["autoTranscriptionGeneration"]))
        return _Call({})

    # conference records
    def conferenceRecords(self):
        return self

    def transcripts(self):
        return _Transcripts(self)


class _Transcripts:
    def __init__(self, meet):
        self.meet = meet

    def list(self, pageSize, pageToken, parent):
        return _Call({"transcripts": self.meet.transcript_lists.get(parent, [])})

    def entries(self):
        meet = self.meet

        class _Entries:
            def list(self, pageSize, pageToken, parent):
                return _Call({"transcriptEntries": meet.entries.get(parent, [])})
        return _Entries()


@pytest.fixture
def meet(monkeypatch):
    fake = FakeMeet()
    monkeypatch.setattr(google_workspace, "meet_client", lambda: fake)
    return fake


@pytest.fixture
def lms(world):
    db = world["db"]
    world["teacher"].workspace_email = "gulzada@mastereducation.kz"
    admin = _user(db, "admin")
    group = world["group"](name="August 19 SAT")

    def lesson(code, days_ahead=1):
        return world["lesson"](group, days_ahead=days_ahead, meeting_url=f"https://meet.google.com/{code}")

    return {"db": db, "admin": admin, "lesson": lesson, "world": world, "group": group}


# ── rooms ────────────────────────────────────────────────────────────────────────────────

def test_switching_on_reaches_every_upcoming_room_once(lms, meet):
    db = lms["db"]
    lms["lesson"]("aaa-bbbb-ccc")
    lms["lesson"]("ddd-eeee-fff", days_ahead=2)
    lms["lesson"]("ggg-hhhh-iii", days_ahead=5)       # beyond the rooms' three days
    lms["lesson"]("jjj-kkkk-lll", days_ahead=-1)      # over
    assert meet_talk_sync.sync_rooms(db) == 0, "off: rooms never switched on are left alone"

    talk_settings.update(db, lms["admin"], enabled=True)
    assert meet_talk_sync.sync_rooms(db) == 2
    assert sorted(meet.patched) == [("spaces/id-aaa-bbbb-ccc", "ON"), ("spaces/id-ddd-eeee-fff", "ON")]
    assert meet_talk_sync.sync_rooms(db) == 0, "already on: not asked again"

    talk_settings.update(db, lms["admin"], enabled=False)
    assert meet_talk_sync.sync_rooms(db) == 2
    assert meet.patched[-2:] == [("spaces/id-aaa-bbbb-ccc", "OFF"), ("spaces/id-ddd-eeee-fff", "OFF")]


def test_a_room_the_robot_cannot_change_is_remembered_a_passing_error_retried(lms, meet):
    db = lms["db"]
    own = lms["lesson"]("own-link-xyz")
    flaky = lms["lesson"]("fla-kyyy-abc")
    meet.refuse = {"own-link-xyz": _HttpError(403), "fla-kyyy-abc": _HttpError(503)}
    talk_settings.update(db, lms["admin"], enabled=True)
    assert meet_talk_sync.sync_rooms(db) == 0
    assert db.get(MeetRoomTranscription, own.id).error
    assert db.get(MeetRoomTranscription, flaky.id) is None, "503: try again next tick"
    del meet.refuse["fla-kyyy-abc"]
    assert meet_talk_sync.sync_rooms(db) == 1


def test_only_lms_rooms_are_touched(lms, meet):
    db = lms["db"]
    lms["world"]["teacher"].workspace_email = None
    lms["lesson"]("aaa-bbbb-ccc")
    talk_settings.update(db, lms["admin"], enabled=True)
    assert meet_talk_sync.sync_rooms(db) == 0 and meet.patched == []


# ── who spoke when ───────────────────────────────────────────────────────────────────────

def _call(lms, ended_minutes_ago=60, synced=True):
    lesson = lms["lesson"]("aaa-bbbb-ccc", days_ahead=-1)
    now = datetime.utcnow()
    call = MeetConference(event_id=lesson.id, conference_record=f"conferenceRecords/c{lesson.id}",
                          started_at=now - timedelta(minutes=ended_minutes_ago + 60),
                          ended_at=now - timedelta(minutes=ended_minutes_ago),
                          synced_at=now if synced else None)
    lms["db"].add(call)
    lms["db"].flush()
    return call


def _entry(who, a, b):
    return {"participant": who, "startTime": f"2026-09-11T15:{a:02d}:00.123456789Z",
            "endTime": f"2026-09-11T15:{b:02d}:30Z", "languageCode": "en-US", "text": "garbled"}


def test_speech_is_saved_as_who_spoke_when_without_the_words(lms, meet):
    db = lms["db"]
    talk_settings.update(db, lms["admin"], enabled=True)
    call = _call(lms)
    t = f"{call.conference_record}/transcripts/t1"
    meet.transcript_lists[call.conference_record] = [{"name": t, "state": "FILE_GENERATED",
                                                 "docsDestination": {"document": "doc-1"}}]
    meet.entries[t] = [_entry("p/teacher", 0, 5), _entry("p/aya", 6, 7), _entry("p/teacher", 8, 9)]
    assert meet_talk_sync.sync_speech(db) == 1
    row = db.query(MeetSpeech).filter_by(conference_id=call.id).one()
    assert row.state == "saved" and row.document_id == "doc-1"
    assert row.participants == ["p/teacher", "p/aya"]
    assert row.entries[0] == [0, 0, 330_000 - 123], "milliseconds from the first word"
    assert "garbled" not in repr(row.entries)
    assert meet_talk_sync.sync_speech(db) == 0, "read once"


def test_a_call_without_a_transcript_is_none_but_not_too_soon(lms, meet):
    db = lms["db"]
    talk_settings.update(db, lms["admin"], enabled=True)
    fresh = _call(lms, ended_minutes_ago=10)
    assert meet_talk_sync.sync_speech(db) == 0, "Google may not have listed it yet"
    fresh.ended_at = datetime.utcnow() - timedelta(minutes=45)
    db.flush()
    assert meet_talk_sync.sync_speech(db) == 1
    assert db.query(MeetSpeech).one().state == "none"


def test_a_transcript_still_being_written_waits(lms, meet):
    db = lms["db"]
    talk_settings.update(db, lms["admin"], enabled=True)
    call = _call(lms)
    meet.transcript_lists[call.conference_record] = [{"name": "t", "state": "STARTED"}]
    assert meet_talk_sync.sync_speech(db) == 0
    call2 = _call(lms, synced=False)
    assert call2 and meet_talk_sync.sync_speech(db) == 0, "attendance first: speech is named through it"


def test_nothing_is_read_while_switched_off(lms, meet):
    _call(lms)
    assert meet_talk_sync.sync_speech(lms["db"]) == 0


# ── the words ────────────────────────────────────────────────────────────────────────────

DEEPGRAM_ANSWER = {
    "metadata": {"duration": 3600.0},
    "results": {"utterances": [
        {"start": 1.0, "end": 2.5, "speaker": 0, "transcript": "Начнём.",
         "words": [{"language": "ru"}]},
        {"start": 3.0, "end": 4.0, "speaker": 1, "transcript": "  ", "words": []},
        {"start": 5.0, "end": 6.0, "speaker": 1, "transcript": "Reading and Writing?",
         "words": [{"language": "en"}, {"language": "en"}, {"language": "en"}]},
    ]},
}


def test_deepgrams_answer_is_kept_compact():
    utterances, languages, seconds = meet_talk_sync.compact_utterances(DEEPGRAM_ANSWER)
    assert utterances == [[1.0, 2.5, 0, "Начнём."], [5.0, 6.0, 1, "Reading and Writing?"]]
    assert languages == {"ru": 1, "en": 3} and seconds == 3600.0


@pytest.fixture
def ready_lesson(lms, monkeypatch, tmp_path):
    db = lms["db"]
    lesson = lms["lesson"]("aaa-bbbb-ccc", days_ahead=-1)
    call = MeetConference(event_id=lesson.id, conference_record="conferenceRecords/x",
                          ended_at=datetime.utcnow(), synced_at=datetime.utcnow())
    db.add(call)
    db.flush()
    db.add(MeetSpeech(conference_id=call.id, event_id=lesson.id, state="saved", origin=datetime.utcnow(),
                      participants=["p"], entries=[[0, 0, 1000]]))
    db.add(LessonRecording(event_id=lesson.id, status="ready", hls_url="x", drive_file_id="drive-1",
                           conference_record="conferenceRecords/x", ingested_at=datetime.utcnow()))
    db.flush()
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"ogg")
    started = datetime(2026, 9, 11, 14, 50)
    monkeypatch.setattr(meet_talk_sync, "_recording_started_at", lambda record, file_id: started)
    monkeypatch.setattr(meet_talk_sync, "_audio_from_drive", lambda file_id, workdir: audio)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    talk_settings.update(db, lms["admin"], enabled=True)
    return {"db": db, "lesson": lesson, "started": started}


def test_a_ready_recording_becomes_words_once(ready_lesson, monkeypatch):
    db = ready_lesson["db"]
    sent = []
    monkeypatch.setattr(meet_talk_sync, "deepgram", lambda audio, key: sent.append(key) or DEEPGRAM_ANSWER)
    assert meet_talk_sync.transcribe_pending(db) == 1
    row = db.query(LessonTranscript).filter_by(event_id=ready_lesson["lesson"].id).one()
    assert row.status == "ready" and row.recording_started_at == ready_lesson["started"]
    assert row.audio_seconds == 3600.0 and len(row.utterances) == 2 and sent == ["dg-key"]
    assert meet_talk_sync.transcribe_pending(db) == 0
    assert talk_settings.usage(db)["lessons"] == 1 and talk_settings.usage(db)["audio_hours"] == 1.0


def test_a_failing_transcript_gives_up_after_three_tries(ready_lesson, monkeypatch):
    db = ready_lesson["db"]

    def broke(audio, key):
        raise RuntimeError("402 Payment Required")
    monkeypatch.setattr(meet_talk_sync, "deepgram", broke)
    for _ in range(3):
        meet_talk_sync.transcribe_pending(db)
    row = db.query(LessonTranscript).one()
    assert (row.status, row.attempts) == ("failed", 3) and "402" in row.error
    assert meet_talk_sync.transcribe_pending(db) == 0
    assert "402" in talk_settings.last_error(db)


@pytest.mark.parametrize("switch", ["talk_off", "transcripts_off", "no_key"])
def test_no_words_without_both_switches_and_a_key(ready_lesson, monkeypatch, switch):
    db = ready_lesson["db"]
    monkeypatch.setattr(meet_talk_sync, "deepgram", lambda audio, key: pytest.fail("Deepgram must not be called"))
    if switch == "talk_off":
        talk_settings.update(db, None, enabled=False)
    elif switch == "transcripts_off":
        talk_settings.update(db, None, transcripts=False)
    else:
        monkeypatch.delenv("DEEPGRAM_API_KEY")
    assert meet_talk_sync.transcribe_pending(db) == 0


def test_lessons_from_before_talk_time_can_be_transcribed_on_request(ready_lesson, monkeypatch):
    db = ready_lesson["db"]
    lesson = ready_lesson["lesson"]
    db.query(MeetSpeech).filter_by(event_id=lesson.id).delete()  # taught while Meet was not transcribing
    db.add(LessonTranscript(event_id=lesson.id, status="failed", attempts=3, error="old"))
    db.flush()
    monkeypatch.setattr(meet_talk_sync, "deepgram", lambda audio, key: DEEPGRAM_ANSWER)
    assert meet_talk_sync.transcribe_pending(db) == 0, "the worker only takes lessons Meet was transcribing"
    assert meet_talk_sync.transcribe_lessons(db, [lesson.id, 999999], "k") == {
        lesson.id: "ready", 999999: "no ready recording"}
    assert meet_talk_sync.transcribe_lessons(db, [lesson.id], "k") == {lesson.id: "already transcribed"}


# ── the words: only the speech goes to Whisper (owner, 2026-09-12) ────────────────────────

def test_speech_is_grouped_into_pieces_with_the_silence_cut_out():
    regions = [(10, 14), (15, 20), (40, 45), (46, 300)]
    chunks = meet_talk_sync.speech_chunks(regions, longest=110)
    assert chunks[:2] == [(10, 20), (40, 45)], "the 20 s silence between them is cut out"
    assert chunks[2:] == [(46, 156), (156, 266), (266, 300)], "a long stretch is cut into sendable pieces"
    assert meet_talk_sync.speech_chunks([(0, 60), (61, 100)]) == [(0, 100)], "a 1 s gap is kept"


def test_punctuation_comes_back_onto_the_timed_words():
    text = "Какой ответ правильный? Думаю, B."
    words = [{"word": "Какой", "start": 0, "end": 0.3}, {"word": "ответ", "start": 0.3, "end": 0.6},
             {"word": "правильный", "start": 0.6, "end": 1.0}, {"word": "Думаю", "start": 1.4, "end": 1.8},
             {"word": "B", "start": 1.8, "end": 2.0}]
    assert [w for _s, _e, w in meet_talk_sync._with_punctuation(text, words)] == [
        "Какой", "ответ", "правильный?", "Думаю,", "B."]


@pytest.mark.parametrize("text, hallucinated", [
    ("Продолжение следует...", True),
    ("Спасибо за внимание, подписывайтесь на канал!", True),
    ("Хорошо. Хорошо. Хорошо. Хорошо.", True),
    ("Хорошо, давайте посмотрим на пятый вопрос.", False),
])
def test_whispers_stock_phrases_over_silence_are_dropped(text, hallucinated):
    assert meet_talk_sync._looks_hallucinated(text) is hallucinated
