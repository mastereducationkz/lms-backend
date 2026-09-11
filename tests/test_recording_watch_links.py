"""A three-hour, one-lesson, login-free link to a recording, issued through the CRM.

Accountants check recordings from the CRM and have no LMS account (owner, 2026-09-11). What
must hold: a link opens only its own lesson, only while it runs, is stored only as a hash, and
only the CRM (service key) can make one.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.events.routes.watch_links import WatchLinkRequest, create_watch_link, open_watch_link
from src.routes.crm_internal import _require_crm_internal_key
from src.schemas.models import LessonRecording, RecordingWatchLink
from src.services import recording_watch_links as links
from src.services.media_tokens import verify_media_token
from src.schemas import models as meet_presence_models
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures


@pytest.fixture
def recorded(world):
    db = world["db"]
    group = world["group"](name="July 8 SAT - Gulzada")
    world["enrol"](group)
    lesson = world["lesson"](group, days_ahead=-1, title="July 8 SAT - Gulzada: Lesson 29")
    db.add(LessonRecording(event_id=lesson.id, status="ready", duration_seconds=3834,
                           hls_url=f"/uploads/videos/recordings/{lesson.id}/master.m3u8",
                           poster_url=f"/uploads/videos/recordings/{lesson.id}/poster.jpg"))
    db.flush()
    return {"db": db, "world": world, "lesson": lesson, "group": group}


def _token(url: str) -> str:
    return url.rsplit("/watch/", 1)[1]


def test_a_link_opens_its_lesson_with_what_the_page_shows(recorded):
    db, lesson = recorded["db"], recorded["lesson"]
    made = links.issue(db, lesson.id, issued_to="buh@mastereducation.kz", issued_role="accountant")
    assert made["url"].startswith("https://lms.mastereducation.kz/watch/")

    page = links.redeem(db, _token(made["url"]))
    assert page["title"] == "July 8 SAT - Gulzada: Lesson 29"
    assert page["groups"] == ["July 8 SAT - Gulzada"]
    assert page["duration_seconds"] == 3834
    assert page["url"].startswith("/uploads/v/") and page["poster_url"].startswith("/uploads/v/")


def test_the_video_token_reaches_this_lesson_and_no_other(recorded):
    db, lesson = recorded["db"], recorded["lesson"]
    page = links.redeem(db, _token(links.issue(db, lesson.id, issued_to=None, issued_role=None)["url"]))
    media_token = page["url"].split("/uploads/v/", 1)[1].split("/", 1)[0]
    assert verify_media_token(media_token, f"videos/recordings/{lesson.id}/720p/seg_001.ts")
    assert verify_media_token(media_token, f"videos/recordings/{lesson.id + 1}/master.m3u8") is None


def test_only_a_hash_is_stored_and_every_open_is_counted(recorded):
    db, lesson = recorded["db"], recorded["lesson"]
    token = _token(links.issue(db, lesson.id, issued_to="buh@mastereducation.kz", issued_role="accountant")["url"])
    row = db.query(RecordingWatchLink).filter(RecordingWatchLink.event_id == lesson.id).one()
    assert token not in (row.token_hash or "") and len(row.token_hash) == 64
    assert row.issued_to == "buh@mastereducation.kz" and row.open_count == 0

    links.redeem(db, token)
    first = row.first_opened_at
    links.redeem(db, token)
    assert row.open_count == 2 and row.first_opened_at == first and row.last_opened_at >= first


def test_a_link_stops_after_three_hours(recorded):
    db, lesson = recorded["db"], recorded["lesson"]
    made_at = datetime.utcnow()
    token = _token(links.issue(db, lesson.id, issued_to=None, issued_role=None, now=made_at)["url"])
    links.redeem(db, token, now=made_at + timedelta(hours=2, minutes=59))
    with pytest.raises(links.LinkExpired):
        links.redeem(db, token, now=made_at + links.LINK_TTL)


def test_no_link_for_a_lesson_with_nothing_to_watch(recorded):
    db, world = recorded["db"], recorded["world"]
    processing = world["lesson"](recorded["group"], days_ahead=-2)
    db.add(LessonRecording(event_id=processing.id, status="pending"))
    retired = world["lesson"](recorded["group"], days_ahead=-3)
    db.add(LessonRecording(event_id=retired.id, status="ready", hls_url=None))  # video removed
    db.flush()
    for lesson_id in (processing.id, retired.id, world["lesson"](recorded["group"], days_ahead=-4).id):
        with pytest.raises(HTTPException) as err:
            create_watch_link(lesson_id, WatchLinkRequest(), db=db)
        assert err.value.status_code == 404


def test_the_public_route_says_expired_apart_from_unknown(recorded):
    db, lesson = recorded["db"], recorded["lesson"]
    old = datetime.utcnow() - links.LINK_TTL - timedelta(minutes=1)
    stale = _token(links.issue(db, lesson.id, issued_to=None, issued_role=None, now=old)["url"])
    with pytest.raises(HTTPException) as err:
        open_watch_link(stale, db=db)
    assert err.value.status_code == 410
    for junk in ("x" * 43, "not a token!", ""):
        with pytest.raises(HTTPException) as err:
            open_watch_link(junk, db=db)
        assert err.value.status_code == 404


def test_only_the_crm_can_ask_for_a_link(monkeypatch):
    monkeypatch.setenv("CRM_INTERNAL_SERVICE_KEY", "the-real-key")
    for wrong in (None, "guess"):
        with pytest.raises(HTTPException) as err:
            _require_crm_internal_key(wrong)
        assert err.value.status_code == 401
    _require_crm_internal_key("the-real-key")


def test_the_page_lists_the_class_beside_the_recording(recorded):
    db, world, lesson = recorded["db"], recorded["world"], recorded["lesson"]
    student = db.query(meet_presence_models.GroupStudent).filter_by(group_id=recorded["group"].id).first()
    page = links.redeem(db, _token(links.issue(db, lesson.id, issued_to=None, issued_role=None)["url"]))
    participants = page["participants"]
    assert participants["state"] in ("none", "no_room", "unavailable")
    assert [s["name"] for s in participants["students"]] == ["student"]
    assert "user_id" not in repr(participants), "a page outside the LMS names people, not accounts"
    assert student is not None
