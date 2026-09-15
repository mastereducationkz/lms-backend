"""A deploy never costs a recording or transcript one of its tries; work out of tries surfaces (2026-09-15).

Against a real database: the give-back is one UPDATE over the rows cut off, and must touch nothing else.
"""
import pytest

from src.schemas.models import LessonRecording, LessonTranscript
from src.services import work_in_flight
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures


class _Session:
    """The fixture's session under the names ``give_back_attempts`` uses; a commit only flushes."""

    def __init__(self, db):
        self._db = db

    def query(self, *args):
        return self._db.query(*args)

    def commit(self):
        self._db.flush()

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _nothing_in_flight():
    work_in_flight._rows.clear()
    yield
    work_in_flight._rows.clear()


@pytest.fixture
def rows(world):
    db = world["db"]
    group = world["group"](name="July 8 SAT - Gulzada")

    def recording(status="pending", attempts=1, error=None):
        lesson = world["lesson"](group, days_ahead=-1)
        row = LessonRecording(event_id=lesson.id, status=status, attempts=attempts, error=error,
                              drive_file_id=f"drive-{lesson.id}")
        db.add(row)
        db.flush()
        return row

    def transcript(status="pending", attempts=1):
        lesson = world["lesson"](group, days_ahead=-1)
        row = LessonTranscript(event_id=lesson.id, status=status, attempts=attempts)
        db.add(row)
        db.flush()
        return row

    return {"db": db, "recording": recording, "transcript": transcript}


def test_a_graceful_stop_gives_back_the_attempt_of_work_cut_off(rows):
    db = rows["db"]
    cut_off, finished_meanwhile, done = rows["recording"](attempts=2), rows["recording"](attempts=1), rows["recording"](attempts=1)
    transcript = rows["transcript"](attempts=3)
    finished_meanwhile.status = "ready"
    db.flush()
    for row in (cut_off, finished_meanwhile, done):
        work_in_flight.begin("recording", row.id)
    work_in_flight.end("recording", done.id)
    work_in_flight.begin("transcript", transcript.id)

    given = work_in_flight.give_back_attempts(lambda: _Session(db))

    assert given == {"recording": 1, "transcript": 1}
    for row in (cut_off, finished_meanwhile, done, transcript):
        db.refresh(row)
    assert (cut_off.attempts, finished_meanwhile.attempts, done.attempts, transcript.attempts) == (1, 1, 1, 2)
    assert work_in_flight.in_flight("recording") == set(), "given back once, not again on a second stop"


def test_a_stop_with_nothing_under_way_opens_no_session():
    assert work_in_flight.give_back_attempts(lambda: pytest.fail("nothing to give back")) == {}


def test_work_that_ran_out_of_attempts_is_marked_failed_with_why(rows):
    db = rows["db"]
    stuck = rows["recording"](attempts=3)
    being_worked_on = rows["recording"](attempts=3)
    still_has_tries = rows["recording"](attempts=1)
    failed_before = rows["recording"](attempts=3, error="Drive answered 403")
    work_in_flight.begin("recording", being_worked_on.id)

    assert work_in_flight.write_off_exhausted(_Session(db), "recording", max_attempts=3) == 2

    assert (stuck.status, stuck.error) == ("failed", work_in_flight.EXHAUSTED_REASON)
    assert failed_before.status == "failed" and failed_before.error == "Drive answered 403", "a real error is kept"
    assert being_worked_on.status == "pending", "not while someone is on it"
    assert still_has_tries.status == "pending"
