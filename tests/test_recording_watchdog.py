"""A lesson running with people in the room and no recording is reported to staff while it is on.

2026-09-16: Laila taught from the Meet app on an iPad on her work account; the app never starts
auto-recording, and nobody knew until the lesson was gone. Google and Telegram are faked; the
lesson, account links and alert rows are real.
"""
from datetime import datetime, timedelta

import pytest

from src.schemas.models import GoogleAccountLink, RecordingStartAlert
from src.services import google_workspace, meet_recordings, recording_watchdog, telegram_service
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures

TEACHER_ACCOUNT = "users/111"
STUDENT_ACCOUNT = "users/222"
CONFERENCE = "conferenceRecords/c1"


class _Call:
    def __init__(self, body):
        self.body = body

    def execute(self):
        return self.body


def _z(value: datetime) -> str:
    return value.isoformat() + "Z"


class FakeMeet:
    """Live calls: who is still in each (and since when), and the recordings each has."""

    def __init__(self):
        self.live = {}        # conference -> space
        self.people = {}      # conference -> [participant]
        self.recs = {}        # conference -> [recording]
        self._level = None

    def conferenceRecords(self):
        self._level = "conference"
        return self

    def participants(self):
        self._level = "participants"
        return self

    def recordings(self):
        self._level = "recordings"
        return self

    def list(self, pageSize=100, pageToken=None, filter=None, parent=None):
        if self._level == "conference":
            assert filter == "end_time IS NULL"
            return _Call({"conferenceRecords": [{"name": n, "space": s} for n, s in self.live.items()]})
        if self._level == "participants":
            assert filter == "latest_end_time IS NULL"
            return _Call({"participants": self.people.get(parent, [])})
        return _Call({"recordings": self.recs.get(parent, [])})


@pytest.fixture
def room(world, monkeypatch):
    db = world["db"]
    group = world["group"](name="IELTS August 1 2026 - Лайла")
    world["enrol"](group)
    lesson = world["lesson"](group, days_ahead=0, title="IELTS August 1 2026 - Лайла: Lesson 26",
                             meeting_url="https://meet.google.com/zhi-wyxo-icz")
    world["teacher"].name = "Жанатбеккызы Лайла"
    meet = FakeMeet()
    meet.live[CONFERENCE] = "spaces/lesson"
    sent = []

    def send(chat, text, reply_to):
        sent.append({"chat": chat, "text": text, "reply_to": reply_to})
        return 100 + len(sent)

    monkeypatch.setattr(google_workspace, "meet_client", lambda: meet)
    monkeypatch.setattr(meet_recordings, "space_meet_code",
                        lambda space: "zhi-wyxo-icz" if space == "spaces/lesson" else None)
    monkeypatch.setenv("ENABLE_RECORDING_WATCHDOG", "true")
    monkeypatch.setenv("TELEGRAM_RECORDING_ALERT_CHATS", "1160156761, -1001792693086:7")

    def in_room(*accounts, since_minutes_after_start=-5):
        joined = _z(lesson.start_datetime + timedelta(minutes=since_minutes_after_start))
        meet.people[CONFERENCE] = [{"signedinUser": {"user": a}, "earliestStartTime": joined} for a in accounts]

    def recording_starts(minutes_after_start):
        meet.recs[CONFERENCE] = [{"state": "STARTED",
                                  "startTime": _z(lesson.start_datetime + timedelta(minutes=minutes_after_start))}]

    def teacher_confirmed():
        db.add(GoogleAccountLink(google_user=TEACHER_ACCOUNT, user_id=world["teacher"].id))
        db.flush()

    def run(minutes_after_start):
        return recording_watchdog.check_recordings_started(
            db, now=lesson.start_datetime + timedelta(minutes=minutes_after_start), send=send)

    return {"db": db, "lesson": lesson, "meet": meet, "sent": sent, "in_room": in_room, "run": run,
            "recording_starts": recording_starts, "teacher_confirmed": teacher_confirmed}


def test_a_lesson_running_without_a_recording_is_reported_once_to_every_staff_chat(room):
    room["teacher_confirmed"]()
    room["in_room"](STUDENT_ACCOUNT, TEACHER_ACCOUNT)

    assert room["run"](4) == 1
    assert [s["chat"] for s in room["sent"]] == ["1160156761", "-1001792693086:7"]
    text = room["sent"][0]["text"]
    assert "Урок идёт без записи" in text
    assert "IELTS August 1 2026 - Лайла: Lesson 26" in text
    assert "Жанатбеккызы Лайла" in text
    assert "В комнате 2 чел." in text
    assert "iPhone/iPad" in text  # the teacher is there, so it is the device or the account
    assert "https://meet.google.com/zhi-wyxo-icz" in text

    assert room["run"](5) == 0 and len(room["sent"]) == 2  # told once, not every minute
    alert = room["db"].query(RecordingStartAlert).filter_by(event_id=room["lesson"].id).one()
    assert alert.teacher_in_room is True and alert.people_in_room == 2
    assert alert.messages == [{"chat": "1160156761", "message_id": 101},
                              {"chat": "-1001792693086:7", "message_id": 102}]


def test_nothing_is_said_in_the_first_three_minutes(room):
    room["in_room"](STUDENT_ACCOUNT)
    assert room["run"](2) == 0
    assert room["sent"] == []


def test_a_lesson_that_is_recording_is_left_alone(room):
    room["in_room"](STUDENT_ACCOUNT, TEACHER_ACCOUNT)
    room["recording_starts"](0)
    assert room["run"](10) == 0
    assert room["sent"] == []


def test_an_empty_room_or_people_who_only_just_arrived_are_not_reported(room):
    assert room["run"](10) == 0  # nobody in the call right now
    room["in_room"](STUDENT_ACCOUNT, since_minutes_after_start=9.5)
    assert room["run"](10) == 0  # joined 30 s ago: the recording may be seconds away
    assert room["run"](11) == 1


def test_when_the_recording_starts_after_the_alert_every_chat_gets_a_reply(room):
    room["in_room"](STUDENT_ACCOUNT, TEACHER_ACCOUNT)
    room["run"](4)
    room["recording_starts"](6)

    assert room["run"](7) == 0
    replies = room["sent"][2:]
    assert [(r["chat"], r["reply_to"]) for r in replies] == [("1160156761", 101), ("-1001792693086:7", 102)]
    assert "Запись началась" in replies[0]["text"] and "6 мин." in replies[0]["text"]

    room["run"](8)
    assert len(room["sent"]) == 4  # resolved once


@pytest.mark.parametrize("confirmed, accounts, expected", [
    (True, (STUDENT_ACCOUNT,), "Учителя в комнате нет"),
    (False, (STUDENT_ACCOUNT, TEACHER_ACCOUNT), "не подтверждены"),
])
def test_the_alert_says_what_is_known_about_the_teacher(room, confirmed, accounts, expected):
    if confirmed:
        room["teacher_confirmed"]()
    room["in_room"](*accounts)
    room["run"](4)
    assert expected in room["sent"][0]["text"]


def test_after_the_lesson_and_outside_lesson_rooms_it_stays_quiet(room):
    room["in_room"](STUDENT_ACCOUNT)
    room["meet"].live["conferenceRecords/other"] = "spaces/someone-else"
    assert room["run"](61) == 0  # over: lingering people are the room closer's business
    assert room["sent"] == []


def test_switched_off_or_without_chats_it_does_not_even_ask_google(room, monkeypatch):
    room["in_room"](STUDENT_ACCOUNT)
    monkeypatch.setattr(google_workspace, "meet_client", lambda: pytest.fail("must not call Google"))
    monkeypatch.setenv("ENABLE_RECORDING_WATCHDOG", "false")
    assert room["run"](10) == 0
    monkeypatch.setenv("ENABLE_RECORDING_WATCHDOG", "true")
    monkeypatch.setenv("TELEGRAM_RECORDING_ALERT_CHATS", "")
    assert room["run"](10) == 0


def test_a_topic_of_a_forum_group_is_addressed_by_thread(monkeypatch):
    posted = []

    class _Response:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 55}}

    monkeypatch.setattr(telegram_service, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(telegram_service.httpx, "post", lambda url, json, timeout: posted.append(json) or _Response())

    assert telegram_service.send_message_sync("-1001792693086:7", "hi", reply_to=9) == 55
    assert telegram_service.send_message_sync("1160156761", "hi") == 55
    assert posted[0]["chat_id"] == "-1001792693086" and posted[0]["message_thread_id"] == 7
    assert posted[0]["reply_parameters"]["message_id"] == 9
    assert "message_thread_id" not in posted[1] and "reply_parameters" not in posted[1]


def test_recording_alert_chats_are_not_the_backup_chats(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RECORDING_ALERT_CHATS", "")
    monkeypatch.setattr(telegram_service, "TELEGRAM_ADMIN_CHAT_IDS", ["1160156761"])
    assert telegram_service.recording_alert_chats() == []
