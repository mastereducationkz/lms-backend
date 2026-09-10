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
4. The Meet space must be created by *us* and set to ``accessType: OPEN``. A space
   Calendar creates is unreadable to this app (403) and defaults to TRUSTED, which would
   have meant students knocking to get in and the poller never matching a recording.

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


class _FakeSpaces:
    def __init__(self, sink):
        self.sink = sink

    def create(self, **kwargs):
        self.sink.append(("spaces.create", kwargs))

        class _R:
            def execute(self):
                return {"name": "spaces/SPACE1",
                        "meetingUri": "https://meet.google.com/abc-defg-hij",
                        "config": {"accessType": "TRUSTED"}}

        return _R()

    def patch(self, **kwargs):
        self.sink.append(("spaces.patch", kwargs))

        class _R:
            def execute(self):
                return {"config": {"accessType": "OPEN"}}

        return _R()


class _FakeMeet:
    def __init__(self, sink):
        self._spaces = _FakeSpaces(sink)

    def spaces(self):
        return self._spaces


@pytest.fixture
def meet_calls(monkeypatch):
    sink = []
    monkeypatch.setattr(meet_scheduling.google_workspace, "meet_client",
                        lambda: _FakeMeet(sink))
    return sink


@pytest.fixture
def calls(monkeypatch, meet_calls):
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


def test_space_is_created_by_us_and_opened(calls, meet_calls):
    """The space must be ours and OPEN, or the pipeline breaks in two silent ways.

    A space Calendar creates returns 403 under the meetings.space.created scope, so the
    poller could never read it to match a conference back to its lesson. And a new space
    defaults to accessType TRUSTED, under which anonymous students must knock — roughly
    ten knocks per lesson.
    """
    meet_scheduling.ensure_meet_link(_FakeDB(), _Event())

    verbs = [v for v, _ in meet_calls]
    assert verbs == ["spaces.create", "spaces.patch"]

    _verb, patch_kwargs = meet_calls[1]
    assert patch_kwargs["body"]["config"]["accessType"] == "OPEN"
    assert patch_kwargs["updateMask"] == "config.accessType"


def test_calendar_attaches_our_space_rather_than_minting_one(calls):
    """Asking Calendar to create the conference is what produced the unreadable space."""
    meet_scheduling.ensure_meet_link(_FakeDB(), _Event())
    conf = calls[0]["body"]["conferenceData"]

    assert "createRequest" not in conf, "Calendar must not mint its own conference"
    assert conf["conferenceSolution"]["key"]["type"] == "hangoutsMeet"
    assert conf["entryPoints"][0]["uri"] == "https://meet.google.com/abc-defg-hij"


def test_no_calendar_event_if_the_space_fails(monkeypatch, calls):
    """Never create a lesson event with no usable conference attached."""
    class _Boom:
        def spaces(self):
            raise RuntimeError("Meet API down")

    monkeypatch.setattr(meet_scheduling.google_workspace, "meet_client", _Boom)
    event = _Event()

    with pytest.raises(meet_scheduling.MeetSchedulingError):
        meet_scheduling.ensure_meet_link(_FakeDB(), event)
    assert calls == [], "must not reach Calendar without a space"
    assert event.meeting_url is None


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


def test_falls_back_to_our_space_uri(monkeypatch, meet_calls):
    """hangoutLink is only populated for Calendar-created conferences.

    Since we now attach our own space, Calendar may return no hangoutLink at all — and the
    space URI we made is the authoritative link regardless.
    """
    sink = []
    monkeypatch.setattr(meet_scheduling.google_workspace, "calendar_client",
                        lambda: _FakeCalendar(sink, {"id": "cal-evt-2"}))  # no hangoutLink
    event = _Event()

    link = meet_scheduling.ensure_meet_link(_FakeDB(), event)

    assert link == "https://meet.google.com/abc-defg-hij"
    assert event.meeting_url == link


def test_api_failure_is_wrapped(monkeypatch, meet_calls):
    class _Boom:
        def events(self):
            raise RuntimeError("quota exceeded")

    monkeypatch.setattr(meet_scheduling.google_workspace, "calendar_client", _Boom)

    with pytest.raises(meet_scheduling.MeetSchedulingError) as excinfo:
        meet_scheduling.ensure_meet_link(_FakeDB(), _Event())
    assert "4242" in str(excinfo.value), "the lesson id must be in the error"
