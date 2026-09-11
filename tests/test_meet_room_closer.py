"""A lesson room left open by a student is closed — never on a teacher who is still teaching.

The rule (owner, 2026-09-11): 15 minutes after the scheduled end and the teacher gone; 60 when
the teacher's accounts are not confirmed yet (their presence cannot be seen). Google is faked;
the lesson and account links are real rows.
"""
from datetime import datetime, timedelta

import pytest

from src.schemas.models import GoogleAccountLink
from src.services import google_workspace, meet_recordings, meet_room_closer
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures

TEACHER_ACCOUNT = "users/111"
STUDENT_ACCOUNT = "users/222"


class _Call:
    def __init__(self, body):
        self.body = body

    def execute(self):
        return self.body


class FakeMeet:
    """Live calls, who is still in each, and every endActiveConference asked for."""

    def __init__(self):
        self.live = {}      # conference name -> (space name, [participants still in])
        self.ended = []

    def conferenceRecords(self):
        return self

    def list(self, pageSize=100, pageToken=None, filter=None, parent=None):
        if parent is None:  # conferenceRecords.list
            assert filter == "end_time IS NULL"
            return _Call({"conferenceRecords": [{"name": n, "space": s} for n, (s, _) in self.live.items()]})
        assert filter == "latest_end_time IS NULL"
        return _Call({"participants": self.live[parent][1]})

    def participants(self):
        return self

    def spaces(self):
        return self

    def endActiveConference(self, name, body):
        self.ended.append(name)
        return _Call({})


@pytest.fixture
def room(world, monkeypatch):
    db = world["db"]
    group = world["group"](name="August 19 SAT - Gulzada")
    world["enrol"](group)
    lesson = world["lesson"](group, days_ahead=-(1 / 24), meeting_url="https://meet.google.com/abc-defg-hij")
    meet = FakeMeet()
    monkeypatch.setattr(google_workspace, "meet_client", lambda: meet)
    monkeypatch.setattr(meet_recordings, "space_meet_code",
                        lambda space: "abc-defg-hij" if space == "spaces/lesson" else None)
    monkeypatch.delenv("ENABLE_AUTO_CLOSE_ROOMS", raising=False)

    def still_in(*accounts):
        meet.live["conferenceRecords/c1"] = ("spaces/lesson", [{"signedinUser": {"user": a}} for a in accounts])

    def teacher_confirmed():
        db.add(GoogleAccountLink(google_user=TEACHER_ACCOUNT, user_id=world["teacher"].id))
        db.flush()

    return {"db": db, "lesson": lesson, "meet": meet, "still_in": still_in, "teacher_confirmed": teacher_confirmed}


def _run(room, minutes_after_end):
    return meet_room_closer.close_lingering_rooms(
        room["db"], now=room["lesson"].end_datetime + timedelta(minutes=minutes_after_end))


def test_a_student_left_behind_is_closed_out_after_fifteen_minutes(room):
    room["teacher_confirmed"]()
    room["still_in"](STUDENT_ACCOUNT)
    assert _run(room, 14) == 0
    assert _run(room, 16) == 1
    assert room["meet"].ended == ["spaces/lesson"]


def test_a_teacher_running_over_is_never_cut_off(room):
    room["teacher_confirmed"]()
    room["still_in"](STUDENT_ACCOUNT, TEACHER_ACCOUNT)
    assert _run(room, 90) == 0
    assert room["meet"].ended == []


def test_an_unseen_teacher_gets_an_hour(room):
    room["still_in"](STUDENT_ACCOUNT)  # the teacher's accounts are not confirmed: cannot tell
    assert _run(room, 30) == 0
    assert _run(room, 61) == 1


def test_rooms_that_are_not_lessons_are_left_alone(room):
    room["meet"].live["conferenceRecords/other"] = ("spaces/someone-else", [])
    room["teacher_confirmed"]()
    assert _run(room, 120) == 0
    assert room["meet"].ended == []


def test_the_switch_turns_it_off(room, monkeypatch):
    room["teacher_confirmed"]()
    room["still_in"](STUDENT_ACCOUNT)
    monkeypatch.setenv("ENABLE_AUTO_CLOSE_ROOMS", "false")
    assert _run(room, 120) == 0
