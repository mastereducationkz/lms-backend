"""Creating the Calendar event that gives a lesson its Meet link.

Three things here are load-bearing enough to pin, because each fails *silently* in
production rather than raising:

1. ``conferenceDataVersion=1``. Omit it and Google returns a valid calendar event with
   no Meet link at all. Lessons would look scheduled and be unrecordable.
2. Students must not appear in ``attendees``. Lessons average ~10 students; inviting them
   would expose every student's personal address to their classmates and generate ~20k
   invitations across the schedule (owner decision, 2026-09-10).
3. The rollout switch. A teacher with no ``workspace_email`` must produce no API call at
   all — that is what keeps the pilot to one teacher instead of all 2,588 future lessons.

No network: the calendar client is replaced with a fake that records the request body.
"""
from datetime import datetime, timedelta

import pytest

from src.services import meet_scheduling


class _FakeEvents:
    def __init__(self, sink, result):
        self._sink = sink
        self._result = result

    def insert(self, **kwargs):
        self._sink.append(kwargs)
        outer = self

        class _Req:
            def execute(self):
                return outer._result

        return _Req()


class _FakeCalendar:
    def __init__(self, sink, result):
        self._events = _FakeEvents(sink, result)

    def events(self):
        return self._events


class _FakeDB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class _Teacher:
    def __init__(self, workspace_email=None):
        self.workspace_email = workspace_email


class _Event:
    def __init__(self, meeting_url=None, workspace_email="gulzada@mastereducation.kz"):
        self.id = 4242
        self.title = "IELTS Speaking — Group A"
        self.description = "Unit 3"
        self.start_datetime = datetime(2026, 9, 15, 10, 0)
        self.end_datetime = datetime(2026, 9, 15, 11, 30)
        self.meeting_url = meeting_url
        self.teacher = _Teacher(workspace_email)


@pytest.fixture
def calls(monkeypatch):
    sink = []
    result = {"id": "cal-evt-1", "hangoutLink": "https://meet.google.com/abc-defg-hij"}
    monkeypatch.setattr(meet_scheduling.google_workspace, "calendar_client",
                        lambda: _FakeCalendar(sink, result))
    return sink


def test_creates_link_and_stores_it(calls):
    db, event = _FakeDB(), _Event()

    link = meet_scheduling.ensure_meet_link(db, event)

    assert link == "https://meet.google.com/abc-defg-hij"
    assert event.meeting_url == link, "the link must be persisted on the lesson"
    assert db.commits == 1


def test_conference_data_version_is_set(calls):
    """Without this, Google returns an event with no Meet link and no error."""
    meet_scheduling.ensure_meet_link(_FakeDB(), _Event())

    assert calls[0]["conferenceDataVersion"] == 1


def test_only_the_teacher_is_invited(calls):
    meet_scheduling.ensure_meet_link(_FakeDB(), _Event())
    body = calls[0]["body"]

    assert body["attendees"] == [{"email": "gulzada@mastereducation.kz"}]
    assert body["guestsCanSeeOtherGuests"] is False
    assert body["guestsCanInviteOthers"] is False


def test_requests_a_real_meet_conference(calls):
    body = meet_scheduling.ensure_meet_link(_FakeDB(), _Event()) and calls[0]["body"]

    req = body["conferenceData"]["createRequest"]
    assert req["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    assert req["requestId"] == "lms-lesson-4242", "must be derived from the lesson id"


def test_request_id_is_stable_across_retries(calls):
    """Google treats requestId as an idempotency key; a retry must not mint a second Meet."""
    first = meet_scheduling._request_id(4242)
    second = meet_scheduling._request_id(4242)

    assert first == second
    assert meet_scheduling._request_id(4243) != first


def test_existing_link_short_circuits(calls):
    event = _Event(meeting_url="https://meet.google.com/already-there")
    db = _FakeDB()

    link = meet_scheduling.ensure_meet_link(db, event)

    assert link == "https://meet.google.com/already-there"
    assert calls == [], "must not call Google when the lesson already has a link"
    assert db.commits == 0


def test_teacher_without_workspace_email_is_skipped(calls):
    """The rollout switch: no workspace_email, no Meet link, no API call."""
    db = _FakeDB()

    assert meet_scheduling.ensure_meet_link(db, _Event(workspace_email=None)) is None
    assert calls == []
    assert db.commits == 0


def test_missing_hangout_link_is_an_error(monkeypatch):
    """A calendar event without a Meet link is a failure, not a success to be stored."""
    sink = []
    monkeypatch.setattr(meet_scheduling.google_workspace, "calendar_client",
                        lambda: _FakeCalendar(sink, {"id": "cal-evt-2"}))  # no hangoutLink
    event = _Event()

    with pytest.raises(meet_scheduling.MeetSchedulingError):
        meet_scheduling.ensure_meet_link(_FakeDB(), event)
    assert event.meeting_url is None, "must not persist a lesson as scheduled with no link"


def test_api_failure_is_wrapped(monkeypatch):
    class _Boom:
        def events(self):
            raise RuntimeError("quota exceeded")

    monkeypatch.setattr(meet_scheduling.google_workspace, "calendar_client", _Boom)

    with pytest.raises(meet_scheduling.MeetSchedulingError) as excinfo:
        meet_scheduling.ensure_meet_link(_FakeDB(), _Event())
    assert "4242" in str(excinfo.value), "the lesson id must be in the error"
