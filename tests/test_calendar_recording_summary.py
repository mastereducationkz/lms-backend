"""The calendar knows which lessons have a recording — status only, and only for its viewer.

The calendar response is cached for 30 seconds per user, so it must never carry a playback
link (those are minted per viewer, per request). And it must answer with the same rule as the
Watch button: a curator browsing another group's lessons must not learn they were recorded.
"""
from src.events.routes.events import get_calendar_events
from src.events.routes.recordings import playback_payload
from src.events.schemas import RecordingSummary
from src.schemas.models import LessonRecording
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


def _calendar(world, user, lesson):
    when = lesson.start_datetime
    return {e.id: e for e in get_calendar_events.__wrapped__(
        year=when.year, month=when.month, db=world["db"], current_user=user)}


def _recorded(world, group, status="ready"):
    ev = world["lesson"](group, days_ahead=-1)
    world["db"].add(LessonRecording(
        event_id=ev.id, status=status, drive_file_id=f"d-{ev.id}", duration_seconds=3834,
        hls_url=f"/uploads/videos/recordings/{ev.id}/master.m3u8" if status == "ready" else None))
    world["db"].flush()
    return ev


def test_the_teacher_sees_the_status_and_duration(world):
    group = world["group"]()
    world["enrol"](group)
    ev = _recorded(world, group)
    summary = _calendar(world, world["teacher"], ev)[ev.id].recording
    assert summary == RecordingSummary(status="ready", duration_seconds=3834)


def test_a_lesson_without_a_recording_has_none(world):
    group = world["group"]()
    world["enrol"](group)
    ev = world["lesson"](group, days_ahead=-1)
    assert _calendar(world, world["teacher"], ev)[ev.id].recording is None


def test_processing_is_reported_as_such(world):
    group = world["group"]()
    world["enrol"](group)
    ev = _recorded(world, group, status="pending")
    assert _calendar(world, world["teacher"], ev)[ev.id].recording.status == "pending"


def test_the_summary_can_never_carry_a_link():
    """Structural: the cached schema has no field a URL could travel in."""
    assert set(RecordingSummary.model_fields) == {"status", "duration_seconds"}


def test_a_head_curator_sees_every_groups_recordings(world):
    group = world["group"]()
    world["enrol"](group)
    ev = _recorded(world, group)
    head = _user(world["db"], "head_curator")
    assert _calendar(world, head, ev)[ev.id].recording.status == "ready"


# --- playback payload ----------------------------------------------------------------------


class _Rec:
    def __init__(self, status="ready", hls="/uploads/videos/recordings/5/master.m3u8",
                 poster="/uploads/videos/recordings/5/poster.jpg"):
        self.status, self.hls_url, self.poster_url, self.duration_seconds = status, hls, poster, 60


def test_ready_carries_signed_video_and_preview():
    payload = playback_payload(_Rec(), viewer_id=7)
    assert payload["status"] == "ready"
    assert payload["url"].startswith("/uploads/v/") and payload["url"].endswith("/master.m3u8")
    assert payload["poster_url"].startswith("/uploads/v/") and payload["poster_url"].endswith("/poster.jpg")


def test_removed_is_said_out_loud_instead_of_a_dead_watch_button():
    assert playback_payload(_Rec(hls=None), viewer_id=7) == {"status": "removed", "url": None}


def test_pending_and_failed_carry_no_link():
    assert playback_payload(_Rec(status="pending", hls=None), viewer_id=7)["url"] is None
    assert playback_payload(_Rec(status="failed", hls=None), viewer_id=7)["url"] is None
