"""The ingest thread: recordings one after another, a pause after a failed try, the tick keeps out (2026-09-15)."""
import pytest

from src.services import recordings_worker
from tests.test_recording_lifecycle import _DB


@pytest.fixture
def worker(monkeypatch):
    """A worker on a fake clock, with the database, the disk and the write-off stood in for."""
    clock = [1000.0]
    held_reports = []
    monkeypatch.setattr(recordings_worker, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(recordings_worker.work_in_flight, "write_off_exhausted", lambda *a, **k: 0)
    monkeypatch.setattr(recordings_worker.recording_ingest, "room_on_disk", lambda: True)
    monkeypatch.setattr(recordings_worker.recordings_status, "ingest_held_for_disk", held_reports.append)
    w = recordings_worker.RecordingIngestWorker(clock=lambda: clock[0])
    return {"worker": w, "clock": clock, "held": held_reports}


def test_it_goes_straight_on_while_recordings_wait(worker, monkeypatch):
    line = [1, 2]

    def take(_db, exclude=None):
        if not line:
            return False
        exclude.add(line.pop(0))
        return True

    monkeypatch.setattr(recordings_worker, "ingest_one_pending", take)
    w = worker["worker"]
    assert [w.step(), w.step(), w.step()] == [True, True, False], "rests only once the line is empty"


def test_a_failed_recording_waits_before_its_next_try(worker, monkeypatch):
    """The tick spaced tries five minutes apart for free; back to back, a passing S3 error would spend all three."""
    offered = []

    def take(_db, exclude=None):
        offered.append(set(exclude))
        if 7 in exclude:
            return False
        exclude.add(7)
        return True  # tried, and it stayed pending: the try failed

    monkeypatch.setattr(recordings_worker, "ingest_one_pending", take)
    w, clock = worker["worker"], worker["clock"]

    assert w.step() is True
    clock[0] += 60
    assert w.step() is False, "a minute later it is still resting"
    clock[0] += recordings_worker.RETRY_AFTER_SECONDS
    assert w.step() is True, "and tried again once the pause is over"


def test_a_full_disk_is_said_once_not_every_poll(worker, monkeypatch):
    monkeypatch.setattr(recordings_worker.recording_ingest, "room_on_disk", lambda: False)
    monkeypatch.setattr(recordings_worker, "ingest_one_pending", lambda *a, **k: pytest.fail("nothing onto a full disk"))
    w = worker["worker"]
    assert [w.step(), w.step()] == [False, False]
    assert worker["held"] == [True]


def test_the_tick_leaves_the_recordings_to_the_ingest_thread(monkeypatch):
    for name in ("tick_started", "step_started", "step_finished", "tick_finished"):
        monkeypatch.setattr(recordings_worker.recordings_status, name, lambda *a, **k: None)
    monkeypatch.setattr(recordings_worker, "SessionLocal", lambda: _DB())
    for name in ("ensure_upcoming_meet_links", "poll_for_recordings"):
        monkeypatch.setattr(recordings_worker, name, lambda _db: 0)
    for name in ("sync_rooms", "sync_speech", "transcribe_pending"):
        monkeypatch.setattr(recordings_worker.meet_talk_sync, name, lambda _db: 0)
    monkeypatch.setattr(recordings_worker.meet_attendance, "sync_if_enabled", lambda _db, progress=None: 0)
    monkeypatch.setattr(recordings_worker.recording_alerts, "sweep_missing_recordings", lambda _db: 0)
    monkeypatch.setattr(recordings_worker, "ingest_pending", lambda _db: pytest.fail("the thread owns ingest"))

    summary = recordings_worker.RecordingsWorker(ingest_in_tick=False).tick()

    assert summary["ingested"] == 0
