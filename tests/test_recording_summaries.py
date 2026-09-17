"""Each lesson's recording in two words, for Meet attendance's list of up to 500 lessons (2026-09-16).

The list says where every lesson's recording is — ready with its length, on its way, failed, none — in
the words the player already speaks, and in a fixed number of queries however long the list is.
"""
from datetime import timedelta

import pytest
from sqlalchemy import event as sa_event

from src.schemas.models import LessonRecording
from src.services import recording_alerts, recording_progress
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures


@pytest.fixture
def room(world):
    """Lessons of a Workspace teacher: each one held in an LMS Meet room."""
    world["teacher"].workspace_email = "gulzada@mastereducation.kz"
    world["db"].flush()
    group = world["group"](name="July 8 SAT - Gulzada")
    count = iter(range(100))

    def lesson(**fields):
        return world["lesson"](group, days_ahead=-1, meeting_url=f"https://meet.google.com/abc-defg-h{next(count):02d}", **fields)

    return {"db": world["db"], "world": world, "group": group, "lesson": lesson}


def _recording(db, lesson, **fields):
    db.add(LessonRecording(event_id=lesson.id, **fields))
    db.flush()


def test_a_recording_row_speaks_the_players_words(room):
    db = room["db"]
    ready, retired, pending, failed = (room["lesson"]() for _ in range(4))
    _recording(db, ready, status="ready", hls_url="/uploads/videos/recordings/1/master.m3u8", duration_seconds=3480)
    _recording(db, retired, status="ready", hls_url=None, duration_seconds=3500)
    _recording(db, pending, status="pending", drive_file_id="drive-1")
    _recording(db, failed, status="failed", drive_file_id="drive-2")

    out = recording_progress.summaries(db, [ready, retired, pending, failed])
    assert out[ready.id] == {"status": "ready", "duration_seconds": 3480}
    assert out[retired.id]["status"] == "removed", "a ready row whose video was retired offers nothing to play"
    assert out[pending.id] == {"status": "pending", "duration_seconds": None}
    assert out[failed.id]["status"] == "failed"


def test_without_a_row_a_lesson_waits_for_google_until_the_grace_runs_out(room):
    db = room["db"]
    lesson = room["lesson"]()
    start, end = lesson.start_datetime, lesson.end_datetime

    def status(moment):
        return recording_progress.summaries(db, [lesson], moment)[lesson.id]

    assert status(start + timedelta(minutes=10)) == {"status": "waiting", "duration_seconds": None}
    assert status(end + timedelta(hours=1))["status"] == "waiting"
    assert status(end + timedelta(hours=recording_alerts.GRACE_HOURS, minutes=1))["status"] == "missing"
    assert status(start - timedelta(minutes=1))["status"] == "missing", "a lesson not yet begun has nothing coming"


def test_it_agrees_with_the_player_for_every_lesson_without_a_row(room):
    db, world = room["db"], room["world"]
    recorded = room["lesson"]()
    own_link = world["lesson"](room["group"], days_ahead=-1)  # the teacher's own Meet link: nothing to read
    other = world["lesson"](room["group"], days_ahead=-1, meeting_url="https://meet.google.com/xyz-abcd-efg",
                            event_type="event")
    moment = recorded.end_datetime + timedelta(hours=1)
    ctx = recording_progress.Context(db, moment)
    out = recording_progress.summaries(db, [recorded, own_link, other], moment)
    for lesson in (recorded, own_link, other):
        assert out[lesson.id]["status"] == recording_progress.without_recording(db, ctx, lesson)[0]
    assert [out[e.id]["status"] for e in (recorded, own_link, other)] == ["waiting", "missing", "missing"]


def test_a_teacher_without_a_workspace_account_has_no_recording_coming(world):
    db = world["db"]
    group = world["group"](name="Pre-SAT - Aisha")
    lesson = world["lesson"](group, days_ahead=-1, meeting_url="https://meet.google.com/abc-defg-hij")
    moment = lesson.end_datetime + timedelta(hours=1)
    assert recording_progress.summaries(db, [lesson], moment)[lesson.id]["status"] == "missing"


def test_the_whole_list_costs_two_queries_however_long_it_is(room):
    db = room["db"]
    lessons = [room["lesson"]() for _ in range(12)]
    for lesson in lessons[:5]:
        _recording(db, lesson, status="ready", hls_url=f"/uploads/videos/recordings/{lesson.id}/master.m3u8",
                   duration_seconds=3600)
    moment = lessons[-1].end_datetime + timedelta(hours=1)
    db.flush()

    statements = []
    bind = db.get_bind()

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    sa_event.listen(bind, "before_cursor_execute", count)
    try:
        out = recording_progress.summaries(db, lessons, moment)
        assert recording_progress.summaries(db, [], moment) == {}
    finally:
        sa_event.remove(bind, "before_cursor_execute", count)

    assert len(out) == 12
    assert [out[e.id]["status"] for e in lessons] == ["ready"] * 5 + ["waiting"] * 7
    assert len(statements) <= 2, statements
