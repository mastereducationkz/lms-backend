"""The public ICS feeds and the Calendar page's subscription endpoints."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.courses.models import GroupStudent
from src.events.calendar_models import CalendarFeedToken
from src.events.routes.calendar_feeds import router
from src.routes.auth import get_current_user_dependency
from src.services import group_calendar
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


@pytest.fixture
def app(world):
    db = world["db"]
    state = {"user": None}
    api = FastAPI()
    api.include_router(router, prefix="/calendar")
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_user_dependency] = lambda: state["user"]
    world.update(client=TestClient(api), state=state)
    return world


def _soon(world, group, days=1, **fields):
    start = datetime.utcnow() + timedelta(days=days)
    return world["lesson"](group, start_datetime=start, end_datetime=start + timedelta(hours=1), **fields)


def test_the_group_feed_needs_its_signature(app):
    group = app["group"](name="IELTS July 8 2026 - Саид")
    app["enrol"](group)                     # a group's lessons count only while it has students
    lesson = _soon(app, group, meeting_url="https://meet.google.com/abc-defg-hij")
    ok = app["client"].get(f"/calendar/feeds/group/{group.id}-{group_calendar.group_sig(group.id)}.ics")
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/calendar")
    assert f"UID:lesson-{lesson.id}@lms.mastereducation.kz" in ok.text
    assert "meet.google.com" not in ok.text
    assert app["client"].get(f"/calendar/feeds/group/{group.id}-0000000000000000.ics").status_code == 404
    assert app["client"].get(f"/calendar/feeds/group/{group.id}.ics").status_code == 404
    other = 987654
    assert app["client"].get(f"/calendar/feeds/group/{other}-{group_calendar.group_sig(other)}.ics").status_code == 404


def test_a_personal_feed_follows_its_token_and_rotation_kills_the_old_one(app):
    db = app["db"]
    group = app["group"](name="SAT September 5")
    student = app["enrol"](group)
    lesson = _soon(app, group)
    app["state"]["user"] = student

    token = app["client"].get("/calendar/feed-token").json()
    assert token["webcal_url"].startswith("webcal://")
    first = db.query(CalendarFeedToken).filter_by(user_id=student.id).one().token
    feed = app["client"].get(f"/calendar/feeds/me/{first}.ics")
    assert feed.status_code == 200 and f"UID:lesson-{lesson.id}@" in feed.text

    rotated = app["client"].post("/calendar/feed-token/rotate").json()
    assert rotated["ics_url"] != token["ics_url"] and rotated["rotated_at"]
    assert app["client"].get(f"/calendar/feeds/me/{first}.ics").status_code == 404
    new = db.query(CalendarFeedToken).filter_by(user_id=student.id).one().token
    assert app["client"].get(f"/calendar/feeds/me/{new}.ics").status_code == 200
    assert app["client"].get("/calendar/feeds/me/not-a-token.ics").status_code == 404


def test_a_teachers_feed_has_the_lessons_they_teach(app):
    db = app["db"]
    group = app["group"](name="July 8 SAT")
    app["enrol"](group)
    mine = _soon(app, group)
    stand_in = _user(db, "teacher")
    handed_off = _soon(app, group, days=2, teacher_id=stand_in.id)
    app["state"]["user"] = app["teacher"]
    app["client"].get("/calendar/feed-token")
    token = db.query(CalendarFeedToken).filter_by(user_id=app["teacher"].id).one().token
    text = app["client"].get(f"/calendar/feeds/me/{token}.ics").text
    assert f"UID:lesson-{mine.id}@" in text and f"UID:lesson-{handed_off.id}@" not in text


def test_subscriptions_list_the_persons_groups_with_links(app):
    db = app["db"]
    mine = app["group"](name="B group")
    app["group"](name="Someone else's")
    student = app["enrol"](mine)
    app["state"]["user"] = student
    body = app["client"].get("/calendar/subscriptions").json()
    assert [row["group_name"] for row in body["groups"]] == ["B group"]
    row = body["groups"][0]
    assert row["google_url"] is None and row["ics_url"].endswith(".ics") and row["webcal_url"].startswith("webcal://")
    assert body["personal"]["ics_url"].endswith(".ics")

    app["state"]["user"] = app["teacher"]
    taught = app["client"].get("/calendar/subscriptions").json()
    assert {row["group_name"] for row in taught["groups"]} == {"B group", "Someone else's"}
    assert db.query(GroupStudent).count() >= 1
