"""Saving who was in each lesson's Meet room: read once, after the call ends, never twice.

Google is faked at the edge (the Meet client and the conference listing); the database is
real, because idempotency is the unique constraints doing their job.
"""
from datetime import datetime, timedelta

import pytest

from src.schemas.models import MeetConference, MeetParticipant, MeetParticipantSession
from src.services import google_workspace, meet_attendance, meet_recordings
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures


class _Call:
    def __init__(self, body):
        self.body = body

    def execute(self):
        return self.body


class _Lister:
    """A Meet ``list`` method over fixed pages, keyed by parent."""

    def __init__(self, key, pages_by_parent):
        self.key, self.pages = key, pages_by_parent

    def list(self, parent, pageSize, pageToken=None):
        pages = self.pages.get(parent, [[]])
        i = int(pageToken or 0)
        body = {self.key: pages[i]}
        if i + 1 < len(pages):
            body["nextPageToken"] = str(i + 1)
        return _Call(body)


class _Participants(_Lister):
    def __init__(self, pages, sessions):
        super().__init__("participants", pages)
        self._sessions = sessions

    def participantSessions(self):
        return self._sessions


class FakeMeet:
    def __init__(self, participant_pages, session_pages):
        self._participants = _Participants(participant_pages, _Lister("participantSessions", session_pages))

    def conferenceRecords(self):
        return self

    def participants(self):
        return self._participants


def test_everyone_in_a_call_with_every_session(monkeypatch):
    call = "conferenceRecords/c1"
    fake = FakeMeet(
        {call: [
            [{"name": f"{call}/participants/1", "signedinUser": {"user": "users/111", "displayName": "Aya"}}],
            [{"name": f"{call}/participants/2", "anonymousUser": {"displayName": "Аяу"}},
             {"name": f"{call}/participants/3", "phoneUser": {"displayName": "+7 *** 12"}}],
        ]},
        {
            f"{call}/participants/1": [[
                {"name": f"{call}/participants/1/participantSessions/a",
                 "startTime": "2026-09-10T14:07:31.123456789Z", "endTime": "2026-09-10T14:12:08Z"},
                {"name": f"{call}/participants/1/participantSessions/b", "startTime": "2026-09-10T14:12:14Z"},
            ]],
            f"{call}/participants/2": [[{"name": f"{call}/participants/2/participantSessions/x"}]],
        },
    )
    monkeypatch.setattr(google_workspace, "meet_client", lambda: fake)

    people = meet_attendance.fetch_people(call)
    assert [(p["kind"], p["google_user"], p["display_name"]) for p in people] == [
        ("signed_in", "users/111", "Aya"), ("guest", None, "Аяу"), ("phone", None, "+7 *** 12")]
    aya = people[0]["sessions"]
    assert aya[0]["joined_at"] == datetime(2026, 9, 10, 14, 7, 31, 123456), "naive UTC, like lesson times"
    assert aya[0]["left_at"] == datetime(2026, 9, 10, 14, 12, 8)
    assert aya[1]["left_at"] is None
    assert people[1]["sessions"] == [], "a session with no start is not a session"


@pytest.fixture
def meet(world, monkeypatch):
    """One lesson room, a Google that lists whatever the test says, and a counter of reads."""
    db = world["db"]
    group = world["group"](name="August 19 SAT - Gulzada")
    lesson = world["lesson"](group, days_ahead=-1, meeting_url="https://meet.google.com/abc-defg-hij")
    start = lesson.start_datetime
    listed, reads, broken = [], [], set()

    monkeypatch.setattr(meet_recordings, "list_recent_conferences", lambda hours=None: list(listed))
    monkeypatch.setattr(meet_recordings, "space_meet_code",
                        lambda space: {"spaces/lesson": "abc-defg-hij"}.get(space))

    def fetch(name):
        reads.append(name)
        if name in broken:
            raise RuntimeError("Google said no")
        return [{"participant_name": f"{name}/participants/1", "kind": "signed_in", "google_user": "users/111",
                 "display_name": "Aya",
                 "sessions": [{"session_name": f"{name}/participants/1/participantSessions/1",
                               "joined_at": start, "left_at": start + timedelta(minutes=60)}]}]

    monkeypatch.setattr(meet_attendance, "fetch_people", fetch)

    def call(name, *, ended_minutes_after_start=65, space="spaces/lesson"):
        conference = {"name": name, "space": space, "startTime": (start - timedelta(minutes=5)).isoformat() + "Z"}
        if ended_minutes_after_start is not None:
            conference["endTime"] = (start + timedelta(minutes=ended_minutes_after_start)).isoformat() + "Z"
        listed.append(conference)

    return {"db": db, "lesson": lesson, "start": start, "call": call, "reads": reads, "broken": broken}


def _saved(db, lesson):
    return (db.query(MeetParticipantSession)
            .join(MeetParticipant, MeetParticipant.id == MeetParticipantSession.participant_id)
            .filter(MeetParticipant.event_id == lesson.id).count())


def test_an_ended_call_is_saved_once_and_never_read_again(meet):
    db, lesson = meet["db"], meet["lesson"]
    meet["call"]("conferenceRecords/lesson")
    later = meet["start"] + timedelta(hours=2)

    assert meet_attendance.sync(db, now=later) == 1
    conference = db.query(MeetConference).filter_by(conference_record="conferenceRecords/lesson").one()
    assert conference.event_id == lesson.id and conference.synced_at == later
    assert _saved(db, lesson) == 1

    assert meet_attendance.sync(db, now=later + timedelta(minutes=5)) == 0
    assert meet["reads"] == ["conferenceRecords/lesson"], "Google is asked once per call"
    assert _saved(db, lesson) == 1


def test_a_call_is_read_only_once_it_has_settled(meet):
    db = meet["db"]
    meet["call"]("conferenceRecords/just-over", ended_minutes_after_start=60)
    just_after = meet["start"] + timedelta(minutes=62)
    assert meet_attendance.sync(db, now=just_after) == 0
    assert meet["reads"] == []
    assert meet_attendance.sync(db, now=just_after + meet_attendance.SETTLE) == 1


def test_calls_in_rooms_that_are_not_lessons_are_ignored(meet):
    db = meet["db"]
    meet["call"]("conferenceRecords/someone-else", space="spaces/not-ours")
    assert meet_attendance.sync(db, now=meet["start"] + timedelta(hours=2)) == 0
    assert db.query(MeetConference).filter_by(conference_record="conferenceRecords/someone-else").count() == 0


def test_one_call_failing_does_not_stop_the_others_and_is_tried_again(meet):
    db, lesson = meet["db"], meet["lesson"]
    meet["call"]("conferenceRecords/broken", ended_minutes_after_start=10)
    meet["call"]("conferenceRecords/fine")
    meet["broken"].add("conferenceRecords/broken")
    later = meet["start"] + timedelta(hours=2)

    assert meet_attendance.sync(db, now=later) == 1
    broken = db.query(MeetConference).filter_by(conference_record="conferenceRecords/broken").one()
    assert broken.synced_at is None

    meet["broken"].clear()
    assert meet_attendance.sync(db, now=later + timedelta(minutes=5)) == 1
    assert _saved(db, lesson) == 2


def test_saving_the_same_people_twice_adds_nothing(meet):
    db, lesson = meet["db"], meet["lesson"]
    conference = MeetConference(event_id=lesson.id, conference_record="conferenceRecords/again")
    db.add(conference)
    db.flush()
    people = [{"participant_name": "conferenceRecords/again/participants/1", "kind": "guest", "google_user": None,
               "display_name": "Аяу", "sessions": [{"session_name": "conferenceRecords/again/participants/1/s/1",
                                                     "joined_at": meet["start"], "left_at": None}]}]
    meet_attendance.save_people(db, conference.id, lesson.id, people, meet["start"])
    meet_attendance.save_people(db, conference.id, lesson.id, people, meet["start"])
    assert db.query(MeetParticipant).filter_by(event_id=lesson.id).count() == 1
    assert _saved(db, lesson) == 1


def test_the_switch_keeps_the_worker_step_off(monkeypatch, meet):
    meet["call"]("conferenceRecords/lesson")
    monkeypatch.delenv("ENABLE_MEET_ATTENDANCE", raising=False)
    assert meet_attendance.sync_if_enabled(meet["db"]) == 0
    assert meet["reads"] == []
    monkeypatch.setenv("ENABLE_MEET_ATTENDANCE", "true")
    assert meet_attendance.sync_if_enabled(meet["db"]) == 1
