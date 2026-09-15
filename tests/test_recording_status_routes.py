"""What a page polls while recordings are on their way, and the retry of a failed one (2026-09-15)."""
from datetime import timedelta

import pytest
from fastapi import HTTPException

from src.events.routes.recording_library import recording_statuses, retry_recording
from src.events.routes.recordings import get_lesson_recording
from src.schemas.models import LessonRecording
from src.services import recording_alerts
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures
from tests.test_recording_library import library  # noqa: F401 - fixture


def _ids(library, *names):
    return ",".join(str(library["lessons"][name].id) for name in names)


def _entry(items, library, name):
    return items[str(library["lessons"][name].id)]


def test_staff_poll_the_cards_still_on_their_way(library):
    db = library["db"]
    admin = _user(db, "admin")
    items = recording_statuses(event_ids=_ids(library, "sat_processing", "ielts_failed", "sat_new"),
                               db=db, current_user=admin)["items"]

    processing = _entry(items, library, "sat_processing")
    assert processing["status"] == "pending" and processing["progress"]["stage"] == "queued"
    assert processing["progress"]["position"] == 1
    assert _entry(items, library, "ielts_failed")["progress"]["stage"] == "failed"
    ready = _entry(items, library, "sat_new")
    assert ready["status"] == "ready" and ready["progress"] is None
    assert ready["poster_url"] and ready["duration_seconds"] == 3834, "a card turning ready gets its preview at once"


def test_a_lesson_the_viewer_may_not_watch_is_simply_absent(library):
    items = recording_statuses(event_ids=_ids(library, "sat_new", "ielts", "ielts_failed"),
                               db=library["db"], current_user=library["ielts_student"])["items"]
    assert list(items) == [str(library["lessons"]["ielts"].id)], "another group's lesson, and a failed one, are not a student's"


def test_malformed_or_too_many_ids_are_refused(library):
    admin = _user(library["db"], "admin")
    for event_ids in ("1,x", ",".join(str(i) for i in range(1, 50))):
        with pytest.raises(HTTPException) as refused:
            recording_statuses(event_ids=event_ids, db=library["db"], current_user=admin)
        assert refused.value.status_code == 400


def test_heads_send_a_failed_recording_round_again(library):
    db = library["db"]
    lesson = library["lessons"]["ielts_failed"]
    recording = db.query(LessonRecording).filter_by(event_id=lesson.id).one()
    recording.attempts, recording.error = 3, "Drive answered 403"
    head = _user(db, "head_teacher")

    entry = retry_recording(event_id=lesson.id, db=db, current_user=head)

    assert (recording.status, recording.attempts, recording.error) == ("pending", 0, None)
    assert entry["status"] == "pending" and entry["progress"]["stage"] == "queued"
    with pytest.raises(HTTPException) as again:
        retry_recording(event_id=lesson.id, db=db, current_user=head)
    assert again.value.status_code == 409, "only a failed recording is retried"


def test_only_heads_retry(library):
    db = library["db"]
    lesson = library["lessons"]["ielts_failed"]
    curator = _user(db, "curator")
    with pytest.raises(HTTPException) as hidden:
        retry_recording(event_id=lesson.id, db=db, current_user=curator)
    assert hidden.value.status_code == 404, "a lesson they may not watch is not confirmed to exist"
    assert db.query(LessonRecording).filter_by(event_id=lesson.id).one().status == "failed"


def test_the_player_says_waiting_until_a_recording_can_no_longer_come(library):
    db, world = library["db"], library["world"]
    world["teacher"].workspace_email = "gulzada@mastereducation.kz"
    admin = _user(db, "admin")

    just_over = world["lesson"](library["sat"], days_ahead=-0.1, meeting_url="https://meet.google.com/wai-tfor-meet")
    response = get_lesson_recording(event_id=just_over.id, db=db, current_user=admin)
    assert response["status"] == "waiting" and response["url"] is None
    assert response["progress"]["stage"] == "waiting_for_google"

    long_ago = world["lesson"](library["sat"], days_ahead=-2, meeting_url="https://meet.google.com/lon-gago-meet")
    assert long_ago.end_datetime + timedelta(hours=recording_alerts.GRACE_HOURS) < just_over.end_datetime
    assert get_lesson_recording(event_id=long_ago.id, db=db, current_user=admin)["status"] == "missing"


def test_the_player_shows_where_a_found_recording_stands(library):
    db = library["db"]
    admin = _user(db, "admin")
    response = get_lesson_recording(event_id=library["lessons"]["sat_processing"].id, db=db, current_user=admin)
    assert response["status"] == "pending" and response["progress"]["stage"] == "queued"
