"""Talk time read back as people — and the switch, the rooms, and the words (owner, 2026-09-11).

The lesson is test_meet_presence's: a teacher, three students, one Meet call, 60 minutes;
times are minutes from the lesson's start. Meet's speech timing is written the way the worker
saves it (``meet_speech``), Deepgram's words the way it saves them (``lesson_transcripts``).
"""
from datetime import timedelta

import pytest
from fastapi import HTTPException

from src.events.routes.meet_attendance import list_lesson_records
from src.events.routes.meet_talk import (
    TalkSettingsIn,
    get_group_talk,
    get_lesson_talk,
    get_talk_settings,
    put_talk_settings,
)
from src.schemas.models import GoogleAccountLink, LessonRecording, LessonTranscript, MeetSpeech
from src.services import meet_talk, meet_talk_stats, talk_settings
from tests.test_meet_presence import room  # noqa: F401 - fixture
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


@pytest.fixture
def talk(room):
    """The room with talk time switched on, and a way to say who spoke when."""
    db = room["db"]
    admin = _user(db, "admin")
    talk_settings.update(db, admin, enabled=True)
    start = room["lesson"].start_datetime

    def spoke(*turns, conference=None):
        """turns: (participant, from_min, to_min) — written as one call's saved speech."""
        call = conference or room["call"]
        names = []
        entries = []
        for participant, a, b in turns:
            if participant.participant_name not in names:
                names.append(participant.participant_name)
            entries.append([names.index(participant.participant_name),
                            int((a + 10) * 60_000), int((b + 10) * 60_000)])
        db.add(MeetSpeech(conference_id=call.id, event_id=call.event_id, state="saved",
                          origin=start - timedelta(minutes=10), participants=names, entries=entries))
        db.flush()

    def words(*utterances, started_min=-10, status="ready"):
        """utterances: (from_min, to_min, voice, text) — recording seconds from its start."""
        db.add(LessonTranscript(
            event_id=room["lesson"].id, status=status, attempts=1,
            recording_started_at=start + timedelta(minutes=started_min),
            utterances=[[(a - started_min) * 60, (b - started_min) * 60, v, t] for a, b, v, t in utterances],
            error="Deepgram said 402" if status == "failed" else None))
        db.flush()

    room.update(admin=admin, spoke=spoke, words=words)
    return room


def _talk(room, user=None):
    return meet_talk.lesson_talk(room["db"], room["lesson"], viewer_role=(user or room["admin"]).role,
                                 now=room["after"])


def _person(talk, name):
    return next(p for p in talk["people"] if p["name"] == name)


# ── the switch ───────────────────────────────────────────────────────────────────────────

def test_switched_off_everything_says_off(talk):
    db = talk["db"]
    talk["spoke"]((talk["link"](g := talk["joined"]("Gulzada", (0, 60)), talk["teacher"]) or g, 0, 30))
    talk_settings.update(db, talk["admin"], enabled=False)
    assert _talk(talk)["state"] == "off"
    listing = list_lesson_records(date_from=None, date_to=None, teacher_id=None, group_id=None,
                                  db=db, current_user=talk["admin"])
    assert listing["talk_enabled"] is False
    assert all(i["talk"] is None for i in listing["items"])
    with pytest.raises(HTTPException) as err:
        get_group_talk(talk["group"].id, date_from=None, date_to=None, db=db, current_user=talk["admin"])
    assert err.value.status_code == 409


def test_heads_read_the_switch_and_only_admins_flip_it(talk):
    db = talk["db"]
    head = _user(db, "head_curator")
    assert get_talk_settings(db=db, current_user=head)["enabled"] is True
    with pytest.raises(HTTPException) as err:
        put_talk_settings(TalkSettingsIn(enabled=False), db=db, current_user=head)
    assert err.value.status_code == 403
    with pytest.raises(HTTPException) as err:
        get_talk_settings(db=db, current_user=talk["teacher"])
    assert err.value.status_code == 404

    out = put_talk_settings(TalkSettingsIn(transcripts=False), db=db, current_user=talk["admin"])
    assert (out["enabled"], out["transcripts"]) == (True, False)
    assert out["enabled_at"].endswith("Z") and out["updated_by"] == talk["admin"].name
    assert out["usage"]["lessons"] == 0


# ── one lesson ───────────────────────────────────────────────────────────────────────────

def _classroom(talk):
    """Teacher 0–60, Aya 0–60, Eldana 0–60 (silent), Shyngys 0–5 (too short to be silent)."""
    teacher = talk["joined"]("Gulzada", (0, 60))
    aya = talk["joined"]("Aya", (0, 60))
    eldana = talk["joined"]("Eldana", (0, 60))
    shyngys = talk["joined"]("Shyngys", (0, 5))
    for account, person in ((teacher, talk["teacher"]), (aya, talk["aya"]), (eldana, talk["eldana"]),
                            (shyngys, talk["shyngys"])):
        talk["link"](account, person)
    return teacher, aya, eldana, shyngys


def test_who_spoke_for_how_long_and_who_did_not(talk):
    teacher, aya, _eldana, _shyngys = _classroom(talk)
    talk["spoke"]((teacher, 0, 20), (aya, 20, 25), (teacher, 25, 40), (aya, 40, 45))
    t = _talk(talk)
    assert t["state"] == "ready"
    assert _person(t, "Гульзада Сапарова")["seconds"] == 35 * 60
    assert _person(t, "Аяулым Сейтова")["seconds"] == 10 * 60
    assert (t["teacher_share"], t["students_share"]) == (0.778, 0.222)
    assert [s["name"] for s in t["silent_students"]] == ["Елдана Нұрлан"], "5 minutes in the room is not silence"
    assert t["people"][0]["role"] == "teacher"
    assert t["speech_seconds"] == 45 * 60 and t["silence_seconds"] == 15 * 60
    assert t["buckets"][0] == {"from_minute": 0, "teacher_seconds": 600, "students_seconds": 0}
    assert t["buckets"][2] == {"from_minute": 20, "teacher_seconds": 300, "students_seconds": 300}
    assert t["longest_teacher_stretch_seconds"] == 20 * 60
    assert _person(t, "Аяулым Сейтова")["spans"] == [[1200.0, 1500.0], [2400.0, 2700.0]]
    assert t["insights"] is None and _person(t, "Аяулым Сейтова")["questions"] is None


def test_an_unconfirmed_voice_counts_for_the_students_until_someone_names_it(talk):
    teacher = talk["joined"]("Gulzada", (0, 60))
    talk["link"](teacher, talk["teacher"])
    phone = talk["joined"]("iPhone 13", (0, 60))
    talk["spoke"]((teacher, 0, 30), (phone, 30, 40))
    t = _talk(talk)
    assert t["held_back"] is True
    assert _person(t, "iPhone 13")["role"] == "unknown" and t["students_share"] == 0.25

    talk["link"](phone, talk["aya"])
    t = _talk(talk)
    assert t["held_back"] is False and _person(t, "Аяулым Сейтова")["seconds"] == 600


def test_a_guest_who_is_not_a_student_takes_no_share(talk):
    teacher = talk["joined"]("Gulzada", (0, 60))
    talk["link"](teacher, talk["teacher"])
    fikrat = talk["joined"]("Fikrat", (0, 60))
    talk["db"].add(GoogleAccountLink(google_user=fikrat.google_user, not_a_student=True))
    talk["db"].flush()
    talk["spoke"]((teacher, 0, 30), (fikrat, 30, 40))
    t = _talk(talk)
    assert _person(t, "Fikrat")["role"] == "other" and _person(t, "Fikrat")["share"] == 0.0
    assert t["teacher_share"] == 1.0


# ── the words ────────────────────────────────────────────────────────────────────────────

def test_each_line_is_named_by_meets_timing(talk):
    teacher, aya, _eldana, _s = _classroom(talk)
    talk["spoke"]((teacher, 0, 10), (aya, 10, 11), (teacher, 11, 20))
    talk["db"].add(LessonRecording(event_id=talk["lesson"].id, status="ready", hls_url="x"))
    talk["words"](
        (0, 1, 0, "Здравствуйте, начнём с задания пять."),
        (1, 9.8, 0, "Какой ответ правильный?"),
        (10, 10.5, 1, "Думаю, B."),
        (10.5, 10.9, 1, "А почему не C?"),
        (11, 12, 0, "Хороший вопрос."),
        (25, 26, 3, "Кто-то вне Meet."),
    )
    t = _talk(talk)
    lines = t["transcript"]["lines"]
    assert t["transcript"]["state"] == "ready"
    assert [(n["speaker_label"], n["role"]) for n in lines] == [
        ("Гульзада Сапарова", "teacher"), ("Аяулым Сейтова", "student"),
        ("Гульзада Сапарова", "teacher"), ("Голос 4", None)]
    assert lines[1]["text"] == "Думаю, B. А почему не C?", "one speaker's utterances are one turn"
    assert lines[1]["lesson_at"] == 600 and lines[1]["at"] == 1200, "the video starts 10 min before the lesson"
    assert t["transcript"]["recording_offset_seconds"] == 600
    assert t["insights"] == {"teacher_questions": 1, "answered": 1, "median_wait_seconds": 12.0,
                             "student_questions": 1}
    assert _person(t, "Аяулым Сейтова")["questions"] == 1


def test_a_voice_meet_missed_is_named_by_the_rest_of_its_speech(talk):
    teacher, aya, _e, _s = _classroom(talk)
    talk["spoke"]((teacher, 0, 10), (aya, 10, 15))
    talk["words"]((10, 12, 1, "Раз."), (12, 14, 1, "Два."), (30, 31, 1, "Три — Meet не слышал."))
    lines = _talk(talk)["transcript"]["lines"]
    assert [n["speaker_label"] for n in lines] == ["Аяулым Сейтова", "Аяулым Сейтова"]
    assert lines[-1]["text"] == "Три — Meet не слышал."


@pytest.mark.parametrize("setup, state", [
    ("recording", "pending"),
    ("recording_transcripts_off", "off"),
    ("failed", "failed"),
    ("nothing", "not_available"),
])
def test_the_transcript_says_where_it_is(talk, monkeypatch, setup, state):
    teacher = talk["joined"]("Gulzada", (0, 60))
    talk["link"](teacher, talk["teacher"])
    talk["spoke"]((teacher, 0, 30))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    if setup.startswith("recording"):
        talk["db"].add(LessonRecording(event_id=talk["lesson"].id, status="ready", hls_url="x"))
        talk["db"].flush()
    if setup == "recording_transcripts_off":
        talk_settings.update(talk["db"], talk["admin"], transcripts=False)
    if setup == "failed":
        talk["words"](status="failed")
    block = _talk(talk)["transcript"]
    assert block["state"] == state
    if setup == "failed":
        assert block["error"] == "Deepgram said 402"
        assert _talk(talk, _user(talk["db"], "head_curator"))["transcript"]["error"] is None


def test_speech_not_in_yet_is_waiting_then_none(talk):
    talk["joined"]("Gulzada", (0, 60))
    assert _talk(talk)["state"] == "waiting"
    later = talk["lesson"].end_datetime + meet_talk.SPEECH_EXPECTED_WITHIN + timedelta(minutes=1)
    assert meet_talk.lesson_talk(talk["db"], talk["lesson"], now=later)["state"] == "none"


# ── who may read it ──────────────────────────────────────────────────────────────────────

def test_the_same_people_as_attendance_and_never_students(talk):
    db = talk["db"]
    teacher, *_ = _classroom(talk)
    talk["spoke"]((teacher, 0, 30))
    curator = _user(db, "curator")
    talk["group"].curator_id = curator.id
    db.flush()
    for viewer in (talk["teacher"], curator, _user(db, "head_teacher")):
        assert get_lesson_talk(talk["lesson"].id, db=db, current_user=viewer)["state"] == "ready"
    for stranger in (talk["aya"], _user(db, "curator"), _user(db, "teacher")):
        with pytest.raises(HTTPException) as err:
            get_lesson_talk(talk["lesson"].id, db=db, current_user=stranger)
        assert err.value.status_code == 404


def test_the_watch_page_gets_minutes_and_the_timeline_but_no_words(talk):
    teacher, aya, *_ = _classroom(talk)
    talk["spoke"]((teacher, 0, 20), (aya, 20, 25))
    talk["words"]((1, 2, 0, "Секретная фраза урока."))
    view = meet_talk.public_talk(_talk(talk))
    assert {p["name"] for p in view["people"]} >= {"Гульзада Сапарова", "Аяулым Сейтова"}
    assert view["silent_students"] == ["Елдана Нұрлан"]
    flat = repr(view)
    for private in ("Секретная", "transcript", "user_id", "key", "insights", "questions"):
        assert private not in flat, private


# ── many lessons ─────────────────────────────────────────────────────────────────────────

def test_the_list_carries_each_lessons_numbers(talk):
    teacher, aya, *_ = _classroom(talk)
    talk["spoke"]((teacher, 0, 20), (aya, 20, 25))
    listing = list_lesson_records(date_from=None, date_to=None, teacher_id=None, group_id=None,
                                  db=talk["db"], current_user=talk["admin"])
    item = next(i for i in listing["items"] if i["event_id"] == talk["lesson"].id)
    assert listing["talk_enabled"] is True
    assert item["talk"]["teacher_share"] == 0.8
    assert item["talk"]["silent"] == [talk["eldana"].id]
    assert item["talk"]["seconds"][str(talk["aya"].id)] == 300


def test_a_group_added_up_over_its_lessons(talk):
    db = talk["db"]
    teacher, aya, eldana, _s = _classroom(talk)
    talk["spoke"]((teacher, 0, 20), (aya, 20, 25))
    talk["db"].add(LessonRecording(event_id=talk["lesson"].id, status="ready", hls_url="x"))
    talk["words"]((20, 21, 1, "Можно вопрос?"))
    out = get_group_talk(talk["group"].id, date_from=None, date_to=None, db=db, current_user=talk["teacher"])
    assert out["totals"]["lessons"] == 1 and out["teacher"]["avg_share"] == 0.8
    by = {s["name"]: s for s in out["students"]}
    assert by["Аяулым Сейтова"]["total_seconds"] == 300 and by["Аяулым Сейтова"]["share_of_student_talk"] == 1.0
    assert by["Аяулым Сейтова"]["questions"] == 1
    assert by["Елдана Нұрлан"]["silent_lessons"] == 1 and by["Елдана Нұрлан"]["lessons_in_room"] == 1
    assert out["lessons"][0]["silent"] == 1

    with pytest.raises(HTTPException) as err:
        get_group_talk(talk["group"].id, date_from=None, date_to=None, db=db, current_user=_user(db, "curator"))
    assert err.value.status_code == 404


def test_one_students_lessons_for_the_report(talk):
    teacher, aya, *_ = _classroom(talk)
    talk["spoke"]((teacher, 0, 20), (aya, 20, 25))
    out = meet_talk_stats.student_talk(talk["db"], talk["aya"].id, now=talk["after"])
    assert out["totals"] == {"lessons": 1, "lessons_spoke": 1, "total_seconds": 300, "avg_seconds": 300,
                             "questions": None}
    assert out["lessons"][0]["share_of_students"] == 1.0 and out["lessons"][0]["in_room"] is True
    talk_settings.update(talk["db"], talk["admin"], enabled=False)
    assert meet_talk_stats.student_talk(talk["db"], talk["aya"].id) is None
