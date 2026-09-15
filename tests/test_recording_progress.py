"""Where a lesson's recording is on its way to the Watch button (2026-09-15).

«Processing» used to cover everything from a lesson ending to its video playing. The stage is read
from the recording row, the ingest line's order and the worker's report — against a real database,
because the line is the worker's own query.
"""
from datetime import datetime, timedelta

import pytest

from src.schemas.models import AppSetting, LessonRecording
from src.services import recording_alerts, recording_progress, recordings_status
from src.utils.utc_json import utc_z
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures

NOW = datetime(2026, 9, 15, 16, 0, 20)


@pytest.fixture
def line(world):
    """Four finished lessons of a Workspace teacher, their recordings found one minute apart."""
    db = world["db"]
    world["teacher"].workspace_email = "gulzada@mastereducation.kz"
    group = world["group"](name="July 8 SAT - Gulzada")
    recs = []
    for i in range(4):
        lesson = world["lesson"](group, days_ahead=-1, meeting_url=f"https://meet.google.com/abc-defg-hi{i}")
        rec = LessonRecording(event_id=lesson.id, status="pending", drive_file_id=f"drive-{i}",
                              created_at=datetime(2026, 9, 15, 15, i))
        db.add(rec)
        db.flush()
        recs.append((rec, lesson))
    return {"db": db, "world": world, "group": group, "recs": recs}


def _report(db, **value):
    row = db.get(AppSetting, recordings_status.KEY)
    if row is None:
        row = AppSetting(key=recordings_status.KEY, value={})
        db.add(row)
    row.value = {**(row.value or {}), **value}
    db.flush()


def _uploading(rec, lesson, *, done=300, total=600, started_ago=60, reported_ago=10):
    return {"recording_id": rec.id, "event_id": lesson.id, "phase": "uploading", "done": done, "total": total,
            "phase_started_at": utc_z(NOW - timedelta(seconds=started_ago)),
            "updated_at": utc_z(NOW - timedelta(seconds=reported_ago))}


def test_each_recording_in_line_knows_its_place(line):
    ctx = recording_progress.Context(line["db"], NOW)
    stages = [recording_progress.of_recording(ctx, rec, lesson) for rec, lesson in line["recs"]]
    assert [(p["stage"], p["position"], p["queue_length"]) for p in stages] == [("queued", i + 1, 4) for i in range(4)]
    assert all(p["max_attempts"] == 3 and p["attempts"] == 0 for p in stages)


def test_the_recording_being_processed_says_its_phase_how_far_and_how_long_is_left(line):
    db = line["db"]
    (first, lesson), (second, second_lesson) = line["recs"][:2]
    _report(db, ingest=_uploading(first, lesson))
    ctx = recording_progress.Context(db, NOW)

    p = recording_progress.of_recording(ctx, first, lesson)
    assert (p["stage"], p["phase"], p["phase_percent"]) == ("processing", "uploading", 50)
    assert p["percent"] == 75, "download .35 + packaging .05 + preview .10, then half the upload"
    assert p["eta_seconds"] == 40, "half took 50 s, so 50 s more — 10 of them already gone since the report"

    nxt = recording_progress.of_recording(ctx, second, second_lesson)
    assert (nxt["stage"], nxt["position"], nxt["queue_length"]) == ("queued", 1, 3), "the one in hand is not in line"


def test_no_time_left_is_guessed_before_there_is_a_rate(line):
    db = line["db"]
    rec, lesson = line["recs"][0]
    _report(db, ingest=_uploading(rec, lesson, done=1, total=600, started_ago=3, reported_ago=1))
    p = recording_progress.of_recording(recording_progress.Context(db, NOW), rec, lesson)
    assert p["phase_percent"] == 0 and p["eta_seconds"] is None


def test_never_a_hundred_percent_before_the_row_says_ready(line):
    db = line["db"]
    rec, lesson = line["recs"][0]
    _report(db, ingest=_uploading(rec, lesson, done=600, total=600))
    assert recording_progress.of_recording(recording_progress.Context(db, NOW), rec, lesson)["percent"] == 99


def test_a_report_left_by_a_stopped_worker_puts_the_recording_back_in_line(line):
    db = line["db"]
    rec, lesson = line["recs"][0]
    rec.attempts = 1
    _report(db, ingest=_uploading(rec, lesson, started_ago=9 * 60, reported_ago=8 * 60))
    p = recording_progress.of_recording(recording_progress.Context(db, NOW), rec, lesson)
    assert (p["stage"], p["position"], p["attempts"]) == ("retrying", 1, 1)


def test_why_it_failed_is_for_staff_only(line):
    db = line["db"]
    rec, lesson = line["recs"][0]
    rec.status, rec.attempts, rec.error = "failed", 3, "ffmpeg exited 1: moov atom not found\nTraceback (most recent call last)"
    ctx = recording_progress.Context(db, NOW)
    staff = recording_progress.of_recording(ctx, rec, lesson, staff=True)
    assert (staff["stage"], staff["error"]) == ("failed", "ffmpeg exited 1: moov atom not found")
    assert recording_progress.of_recording(ctx, rec, lesson, staff=False)["error"] is None


def test_a_ready_recording_needs_no_progress_and_a_retired_one_says_so(line):
    db = line["db"]
    rec, lesson = line["recs"][0]
    rec.status, rec.hls_url = "ready", "/uploads/videos/recordings/1/master.m3u8"
    ctx = recording_progress.Context(db, NOW)
    assert recording_progress.of_recording(ctx, rec, lesson) is None
    rec.hls_url = None
    assert recording_progress.of_recording(ctx, rec, lesson)["stage"] == "removed"


def test_a_line_held_for_a_full_disk_says_so(line):
    db = line["db"]
    rec, lesson = line["recs"][0]
    _report(db, held_for_disk=True)
    assert recording_progress.of_recording(recording_progress.Context(db, NOW), rec, lesson)["held_for_disk"] is True


def test_before_a_recording_is_found_the_lesson_says_what_it_waits_for(line):
    db, world = line["db"], line["world"]
    lesson = world["lesson"](line["group"], days_ahead=-1, meeting_url="https://meet.google.com/new-less-onx")
    start, end = lesson.start_datetime, lesson.end_datetime

    def read(moment, staff=False):
        return recording_progress.without_recording(db, recording_progress.Context(db, moment), lesson, staff=staff)

    assert read(start + timedelta(minutes=10))[1]["stage"] == "lesson_running"
    status, progress = read(end + timedelta(hours=1), staff=True)
    assert (status, progress["stage"]) == ("waiting", "waiting_for_google")
    assert progress["missing_after"] == utc_z(end + timedelta(hours=recording_alerts.GRACE_HOURS))
    assert read(end + timedelta(hours=1))[1]["missing_after"] is None, "only staff are told when it is flagged"
    assert read(end + timedelta(hours=recording_alerts.GRACE_HOURS, minutes=1)) == ("missing", None)


def test_a_lesson_this_pipeline_does_not_record_is_simply_missing(line):
    db, world = line["db"], line["world"]
    own_link = world["lesson"](line["group"], days_ahead=-1)
    moment = own_link.end_datetime + timedelta(hours=1)
    assert recording_progress.without_recording(db, recording_progress.Context(db, moment), own_link) == ("missing", None)
