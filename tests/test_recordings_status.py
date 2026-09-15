"""The recordings worker's status as the pages read it (2026-09-15): checking now, which step, how far, when next.

A bare «Loading» read as a slow LMS while lessons were really waiting on Google Meet and the worker's next check.
"""
from datetime import datetime, timedelta

import src.config
from src.services import recordings_status

NOW = datetime(2026, 9, 15, 16, 40)


class _Row:
    def __init__(self, value):
        self.value = value


class _DB:
    def __init__(self, value=None):
        self._row = _Row(value) if value is not None else None

    def get(self, model, key):
        assert key == recordings_status.KEY
        return self._row


def _ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat() + "Z"


def test_nothing_to_say_before_the_worker_has_ever_run():
    assert recordings_status.snapshot(_DB(), NOW) is None


def test_a_check_under_way_says_its_step_and_how_far_it_has_got():
    sync = recordings_status.snapshot(_DB({
        "started_at": _ago(3), "finished_at": _ago(10), "step": "attendance",
        "progress": {"done": 12, "total": 38}, "poll_seconds": 300}), NOW)
    assert sync["running"] is True and sync["slow"] is False
    assert sync["step"] == "attendance" and sync["progress"] == {"done": 12, "total": 38}
    assert sync["next_at"] is None, "no next check is promised while one is running"


def test_between_checks_it_says_when_the_last_ended_and_the_next_starts():
    sync = recordings_status.snapshot(_DB({
        "started_at": _ago(9), "finished_at": _ago(4), "attendance_at": _ago(5),
        "step": "attendance", "progress": {"done": 1, "total": 2}, "poll_seconds": 300}), NOW)
    assert sync["running"] is False and sync["step"] is None and sync["progress"] is None
    assert recordings_status._parse(sync["attendance_at"]) == NOW - timedelta(minutes=5)
    assert recordings_status._parse(sync["next_at"]) == NOW + timedelta(minutes=1)


def test_a_check_running_far_too_long_is_called_slow():
    """Also what a restart mid-check looks like: a start with no finish after it."""
    sync = recordings_status.snapshot(_DB({"started_at": _ago(40), "finished_at": _ago(60), "step": "claimed"}), NOW)
    assert sync["running"] is True and sync["slow"] is True


def test_status_that_cannot_be_saved_never_stops_the_work(monkeypatch):
    class _Down:
        def get(self, *args):
            raise RuntimeError("database down")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(src.config, "SessionLocal", lambda: _Down())
    recordings_status.step_started("attendance")
    recordings_status.attendance_progress(1, 2)
    recordings_status.tick_finished(12.4)
