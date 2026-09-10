"""Binding a finished Meet recording to the lesson it came from.

The join is the whole point of this module, and the spec is emphatic that it must be
explicit rather than inferred from timestamps or filenames — back-to-back lessons and
rescheduled events make that guesswork (§4.3). So the tests concentrate on:

* matching on the **meeting code**, not the whole URL, because Calendar and the Meet API
  hand back different shapes of the same link;
* treating "recording not finished yet" as an ordinary state rather than a failure, since
  Meet publishes the conference record before the file exists;
* **copy, never move** — moving a file out of Meet's recordings folder is reported to
  revert, which would silently undo the archive;
* claiming a recording exactly once, because polling is at-least-once by nature.

No network anywhere: the Meet and Drive clients are fakes.
"""
from datetime import datetime

import pytest

from src.services import meet_recordings


# --- meeting code extraction -------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://meet.google.com/abc-defg-hij", "abc-defg-hij"),
    ("https://meet.google.com/abc-defg-hij?hs=224", "abc-defg-hij"),
    ("https://meet.google.com/ABC-DEFG-HIJ", "abc-defg-hij"),
    ("http://meet.google.com/abc-defg-hij", "abc-defg-hij"),
])
def test_meet_code_survives_url_variation(url, expected):
    """Calendar's hangoutLink and Meet's meetingUri differ; the code is the invariant."""
    assert meet_recordings.meet_code(url) == expected


@pytest.mark.parametrize("url", [None, "", "https://zoom.us/j/123", "not a url"])
def test_meet_code_none_for_non_meet(url):
    assert meet_recordings.meet_code(url) is None


# --- resolving a conference to a Drive file ----------------------------------

class _FakeRecordings:
    def __init__(self, payload):
        self._payload = payload

    def list(self, **kwargs):
        payload = self._payload

        class _R:
            def execute(self):
                return payload

        return _R()


class _FakeConferenceRecords:
    def __init__(self, payload):
        self._recordings = _FakeRecordings(payload)

    def recordings(self):
        return self._recordings


class _FakeMeet:
    def __init__(self, payload):
        self._cr = _FakeConferenceRecords(payload)

    def conferenceRecords(self):
        return self._cr


def _meet(monkeypatch, payload):
    monkeypatch.setattr(meet_recordings.google_workspace, "meet_client",
                        lambda: _FakeMeet(payload))


def test_resolve_returns_drive_file_id(monkeypatch):
    _meet(monkeypatch, {"recordings": [
        {"name": "conferenceRecords/c1/recordings/r1",
         "driveDestination": {"file": "drive-file-123"}},
    ]})

    assert meet_recordings.resolve_recording("conferenceRecords/c1") == "drive-file-123"


def test_no_recording_yet_is_not_a_failure(monkeypatch):
    """A lesson that just ended has a conference record but no recording. Come back later."""
    _meet(monkeypatch, {"recordings": []})

    with pytest.raises(meet_recordings.RecordingNotReady):
        meet_recordings.resolve_recording("conferenceRecords/c1")


def test_recording_still_processing_is_not_a_failure(monkeypatch):
    """Meet publishes the recording before driveDestination.file is populated."""
    _meet(monkeypatch, {"recordings": [{"name": "conferenceRecords/c1/recordings/r1"}]})

    with pytest.raises(meet_recordings.RecordingNotReady):
        meet_recordings.resolve_recording("conferenceRecords/c1")


# --- matching a conference back to a lesson ----------------------------------

class _FakeQuery:
    def __init__(self, result):
        self._result = result
        self.filters = []

    def filter(self, *a):
        self.filters.append(a)
        return self

    def order_by(self, *a):
        return self

    def first(self):
        return self._result


class _FakeDB:
    def __init__(self, result=None):
        self._result = result
        self.added = []
        self.commits = 0
        self.queries = []

    def query(self, model):
        q = _FakeQuery(self._result)
        self.queries.append((model, q))
        return q

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1


def test_match_lesson_ignores_empty_code():
    """A conference we could not read a code for must not match an arbitrary lesson."""
    db = _FakeDB(result="some-event")

    assert meet_recordings.match_lesson(db, None) is None
    assert db.queries == [], "must not even query without a code"


def test_unreadable_space_is_skipped_quietly(monkeypatch):
    """Someone's ad-hoc meeting is not ours; skip it rather than raising."""
    class _Boom:
        def spaces(self):
            raise RuntimeError("403")

    monkeypatch.setattr(meet_recordings.google_workspace, "meet_client", _Boom)

    assert meet_recordings.space_meet_code("spaces/not-ours") is None


# --- copying into the Shared Drive -------------------------------------------

class _FakeFiles:
    def __init__(self, sink):
        self.sink = sink

    def copy(self, **kwargs):
        self.sink.append(("copy", kwargs))

        class _R:
            def execute(self):
                return {"id": "shared-copy-1"}

        return _R()

    def update(self, **kwargs):  # a move would come through here
        self.sink.append(("update", kwargs))
        raise AssertionError("must never move the file out of Meet's folder")


class _FakeDrive:
    def __init__(self, sink):
        self._files = _FakeFiles(sink)

    def files(self):
        return self._files


class _Event:
    id = 77
    start_datetime = datetime(2026, 9, 15, 10, 0)


def test_copy_into_shared_drive(monkeypatch):
    """Where the file goes is recording_archive's job; this is about how it gets there."""
    from src.services import recording_archive

    sink = []
    monkeypatch.setattr(meet_recordings.google_workspace, "drive_client",
                        lambda: _FakeDrive(sink))
    monkeypatch.setattr(recording_archive, "ensure_lesson_folder", lambda ev: "GROUPFOLDER")

    new_id = meet_recordings.copy_to_shared_drive("orig-file", _Event())

    assert new_id == "shared-copy-1"
    verb, kwargs = sink[0]
    assert verb == "copy", "copy, never move — a move out of Meet's folder reverts"
    assert kwargs["supportsAllDrives"] is True, "Shared Drives are invisible without this"
    assert kwargs["body"]["parents"] == ["GROUPFOLDER"], "Teacher/Group, not the root"
    assert "[77]" in kwargs["body"]["name"], "the event id ties the file back to the LMS"


def test_copy_still_lands_somewhere_when_the_folder_tree_fails(monkeypatch):
    """A misfiled archive is untidy; a missing one lets retention delete the last copy."""
    from src.services import recording_archive

    sink = []
    monkeypatch.setattr(meet_recordings.google_workspace, "drive_client",
                        lambda: _FakeDrive(sink))

    def boom(ev):
        raise RuntimeError("Drive is down")

    monkeypatch.setattr(recording_archive, "_find_or_create_folder",
                        lambda *a, **k: boom(None))

    new_id = meet_recordings.copy_to_shared_drive("orig-file", _Event())

    assert new_id == "shared-copy-1"
    _verb, kwargs = sink[0]
    assert kwargs["body"]["parents"] == [
        meet_recordings.google_workspace.RECORDINGS_SHARED_DRIVE_ID]


# --- claiming exactly once ---------------------------------------------------

def test_claim_creates_one_row():
    db = _FakeDB(result=None)

    row = meet_recordings.claim_recording(db, _Event(), "conferenceRecords/c1", "file-1")

    assert row is not None
    assert len(db.added) == 1
    assert db.added[0].event_id == 77
    assert db.added[0].status == "pending"


def test_claim_is_idempotent():
    """Polling re-sees the same conference every tick; that must not make a second row."""
    class _Existing:
        drive_file_id = "file-1"

    db = _FakeDB(result=_Existing())

    assert meet_recordings.claim_recording(db, _Event(), "conferenceRecords/c1", "file-1") is None
    assert db.added == [], "must not insert a duplicate for an already-claimed lesson"


def test_claim_backfills_a_pending_row():
    """A row created before the file existed gets its ids filled in, not duplicated."""
    class _Existing:
        drive_file_id = None
        conference_record = None

    existing = _Existing()
    db = _FakeDB(result=existing)

    assert meet_recordings.claim_recording(db, _Event(), "conferenceRecords/c9", "file-9") is None
    assert existing.drive_file_id == "file-9"
    assert existing.conference_record == "conferenceRecords/c9"
    assert db.added == []
    assert db.commits == 1
