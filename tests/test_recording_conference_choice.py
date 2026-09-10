"""One lesson, several conferences: which recording actually gets claimed.

A Meet space opens a **new conference every time the room goes from empty to occupied**,
and with auto-recording each one produces its own finished Drive file. The common case is
a student who joins early and leaves before the teacher arrives: that is a complete,
valid, useless recording of an empty room, and because it ends first it also lands in
Drive first. Claiming it would discard the real lesson silently.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.services import meet_recordings, recordings_worker


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Lesson:
    def __init__(self, id=14156, ended_minutes_ago=90):
        self.id = id
        self.end_datetime = _now() - timedelta(minutes=ended_minutes_ago)
        self.start_datetime = self.end_datetime - timedelta(hours=1)
        self.meeting_url = "https://meet.google.com/nee-tsrk-vap"


class _DB:
    """Enough of a session to answer 'is this lesson already claimed?'."""

    def __init__(self, existing=False):
        self.existing = existing
        self.added = []
        self.committed = 0

    def query(self, model):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return object() if self.existing else None

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass


@pytest.fixture
def meet(monkeypatch):
    """Wire a lesson, its conferences, and each conference's recording."""

    def setup(conferences, lesson=None, durations=None, not_ready=(), never_recorded=()):
        lesson = lesson or _Lesson()
        monkeypatch.setattr(recordings_worker.meet_recordings, "list_recent_conferences",
                            lambda: [{"name": n, "space": "spaces/S"} for n in conferences])
        monkeypatch.setattr(recordings_worker.meet_recordings, "space_meet_code",
                            lambda space: "nee-tsrk-vap")
        monkeypatch.setattr(recordings_worker.meet_recordings, "match_lesson",
                            lambda db, code: lesson)

        def detail(name):
            if name in never_recorded:
                raise meet_recordings.NoRecording(name)
            if name in not_ready:
                raise meet_recordings.RecordingNotReady(name)
            return f"file-{name}", (durations or {}).get(name, 0.0)

        monkeypatch.setattr(recordings_worker.meet_recordings,
                            "resolve_recording_detail", detail)

        claims = []
        monkeypatch.setattr(recordings_worker.meet_recordings, "claim_recording",
                            lambda db, ev, conf, fid: claims.append((ev.id, conf, fid)) or True)
        return claims, lesson

    return setup


def test_the_longest_recording_wins_not_the_first_to_land(meet):
    """The scenario: someone joins early, leaves, then the real lesson happens.

    The early conference ends first and renders first. Claiming by arrival order claims
    the empty room and — because claiming is idempotent per lesson — throws the real
    lesson away with no error anywhere.
    """
    claims, _ = meet(
        conferences=["conf-early", "conf-lesson"],
        durations={"conf-early": 120.0, "conf-lesson": 3600.0},
    )

    assert recordings_worker.poll_for_recordings(_DB()) == 1
    assert len(claims) == 1
    _event_id, conference, file_id = claims[0]
    assert conference == "conf-lesson", "claimed the empty room instead of the lesson"
    assert file_id == "file-conf-lesson"


def test_arrival_order_does_not_decide(meet):
    """Same two conferences, listed the other way round — same answer."""
    claims, _ = meet(
        conferences=["conf-lesson", "conf-early"],
        durations={"conf-early": 120.0, "conf-lesson": 3600.0},
    )
    recordings_worker.poll_for_recordings(_DB())
    assert claims[0][1] == "conf-lesson"


def test_nothing_is_claimed_while_the_lesson_could_still_be_running(meet):
    """Mid-lesson, the only finished conference is the early joiner's empty room.

    Claiming then would lock that in before the real recording exists.
    """
    claims, _ = meet(
        conferences=["conf-early"],
        lesson=_Lesson(ended_minutes_ago=-30),  # ends 30 minutes from now
        durations={"conf-early": 120.0},
    )
    assert recordings_worker.poll_for_recordings(_DB()) == 0
    assert claims == []


def test_waits_for_a_sibling_that_is_still_rendering(meet):
    """A conference Meet has not finished rendering might be the real lesson."""
    claims, _ = meet(
        conferences=["conf-early", "conf-lesson"],
        durations={"conf-early": 120.0},
        not_ready=("conf-lesson",),
    )
    assert recordings_worker.poll_for_recordings(_DB()) == 0
    assert claims == [], "must not settle for the short one while the long one renders"


def test_patience_runs_out_so_one_stuck_render_cannot_strand_a_lesson(meet):
    """Waiting forever would be its own failure mode."""
    claims, _ = meet(
        conferences=["conf-early", "conf-stuck"],
        lesson=_Lesson(ended_minutes_ago=60 * (recordings_worker.PATIENCE_HOURS + 1)),
        durations={"conf-early": 120.0},
        not_ready=("conf-stuck",),
    )
    assert recordings_worker.poll_for_recordings(_DB()) == 1
    assert claims[0][1] == "conf-early", "take the best in hand rather than nothing"


def test_a_call_that_never_recorded_does_not_hold_up_the_lesson(meet):
    """Lesson 14156 exactly: two morning test calls in the same room recorded nothing.

    Read as "still rendering", they kept the real 64-minute recording unclaimed for the whole
    patience window — four hours after a lesson whose file was sitting in Drive.
    """
    claims, _ = meet(
        conferences=["conf-morning-test", "conf-midday-test", "conf-lesson"],
        durations={"conf-lesson": 3834.0},
        never_recorded=("conf-morning-test", "conf-midday-test"),
    )
    assert recordings_worker.poll_for_recordings(_DB()) == 1
    assert claims[0][1] == "conf-lesson"


def test_students_alone_before_the_teacher_do_not_delay_it_either(meet):
    """The everyday version: students open the room early, nobody from staff yet, no recording."""
    claims, _ = meet(
        conferences=["conf-students-early", "conf-lesson"],
        durations={"conf-lesson": 3600.0},
        never_recorded=("conf-students-early",),
    )
    assert recordings_worker.poll_for_recordings(_DB()) == 1


def test_an_already_claimed_lesson_is_left_alone(meet):
    claims, _ = meet(conferences=["conf-a"], durations={"conf-a": 3600.0})
    assert recordings_worker.poll_for_recordings(_DB(existing=True)) == 0
    assert claims == []


def test_the_ordinary_single_conference_lesson_still_works(meet):
    claims, _ = meet(conferences=["conf-only"], durations={"conf-only": 3300.0})
    assert recordings_worker.poll_for_recordings(_DB()) == 1
    assert claims[0][1] == "conf-only"


# --- duration parsing -------------------------------------------------------

def test_duration_is_read_from_meets_timestamps(monkeypatch):
    """Meet returns Z-suffixed times with more fractional digits than fromisoformat takes."""
    class _Recs:
        def list(self, **kw):
            class _R:
                def execute(self):
                    return {"recordings": [{
                        "driveDestination": {"file": "F1"},
                        "startTime": "2026-09-10T08:34:41.503231Z",
                        "endTime": "2026-09-10T09:35:11.335535Z",
                    }]}
            return _R()

    class _Conf:
        def recordings(self):
            return _Recs()

    class _Client:
        def conferenceRecords(self):
            return _Conf()

    monkeypatch.setattr(meet_recordings.google_workspace, "meet_client", lambda: _Client())

    file_id, seconds = meet_recordings.resolve_recording_detail("conferenceRecords/X")
    assert file_id == "F1"
    assert 3600 < seconds < 3640, f"expected about an hour, got {seconds}"


def test_a_recording_with_no_file_yet_is_not_ready(monkeypatch):
    class _Recs:
        def list(self, **kw):
            class _R:
                def execute(self):
                    return {"recordings": [{"driveDestination": {}, "state": "ENDED"}]}
            return _R()

    class _Conf:
        def recordings(self):
            return _Recs()

    class _Client:
        def conferenceRecords(self):
            return _Conf()

    monkeypatch.setattr(meet_recordings.google_workspace, "meet_client", lambda: _Client())

    with pytest.raises(meet_recordings.RecordingNotReady):
        meet_recordings.resolve_recording_detail("conferenceRecords/X")
