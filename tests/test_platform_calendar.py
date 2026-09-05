"""Weekly sets as calendar events (lead decision 2026-09-05): one ``weekly_test`` event per
published set, attached to the target groups, linking to the set page on the platform —
instead of platform_test homework rows."""

from datetime import datetime

import pytest
from sqlalchemy import ARRAY, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.courses.models import Group
from src.events.models import Event, EventGroup
from src.integrations import platform_calendar as pc
from src.integrations.models import PlatformTestEvent, PlatformWeeklySet


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    return "JSON"


NOW = datetime(2026, 9, 5, 10, 0)
MODULES = [{"module": "verbal", "test_id": 501, "test_title": "V", "path": "/module/verbal/1?testId=501"},
           {"module": "math", "test_id": 502, "test_title": "M", "path": "/module/math/1?testId=502"}]


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, Group, Event, EventGroup, PlatformWeeklySet, PlatformTestEvent):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_ASSIGNMENTS_ENABLED", "true")


@pytest.fixture()
def admin(db):
    u = UserInDB(email="admin@x.io", name="Admin", role="admin", hashed_password="h", is_active=True)
    db.add(u)
    db.commit()
    return u


def _set(db, set_id=59, platform="sat", track="sat", title="Weekly Set (05.09-06.09)",
         d_from=datetime(2026, 9, 4, 19, 0), d_to=datetime(2026, 9, 6, 18, 59, 59), active=True, set_path=None):
    ws = PlatformWeeklySet(platform=platform, weekly_set_id=set_id, title=title, date_from=d_from, date_to=d_to,
                           is_active=active, track=track, modules=MODULES, set_path=set_path)
    db.add(ws)
    db.commit()
    return ws


def _group(db, name, program="sat", active=True, special=False, opt_out=False):
    g = Group(name=name, program_type=program, is_active=active, is_special=special, platform_tests_opt_out=opt_out)
    db.add(g)
    db.commit()
    return g


def _event(db):
    return db.query(Event).one()


def _group_ids(db, event):
    return sorted(eg.group_id for eg in db.query(EventGroup).filter(EventGroup.event_id == event.id).all())


# --- creation -------------------------------------------------------------------------

def test_publish_creates_one_weekly_test_event_for_the_target_groups(db, admin):
    a = _group(db, "SAT-A")
    b = _group(db, "SAT-B")
    _group(db, "SAT-out", opt_out=True)
    _group(db, "SAT-special", special=True)
    _group(db, "IELTS-A", program="ielts")
    ws = _set(db, set_path="/weekly-sets/59")

    out = pc.sync_weekly_set_event(db, ws, now=NOW)

    assert out == {"created": 1, "updated": 0, "deactivated": 0, "groups_added": 2, "groups_removed": 0}
    ev = _event(db)
    assert ev.event_type == "weekly_test" and ev.is_active is True and ev.is_online is True
    assert ev.title == "SAT Weekly Test · Weekly Set (05.09-06.09)"
    assert (ev.start_datetime, ev.end_datetime) == (ws.date_from, ws.date_to)
    assert ev.meeting_url == "https://sat.mastereducation.kz/weekly-sets/59"
    assert ev.created_by == admin.id
    assert "Verbal, Math" in ev.description
    assert _group_ids(db, ev) == [a.id, b.id]
    link = db.query(PlatformTestEvent).one()
    assert (link.event_id, link.platform, link.weekly_set_id) == (ev.id, "sat", 59)


def test_ielts_set_links_to_the_ielts_set_page_by_default(db, admin):
    _group(db, "IELTS-A", program="ielts")
    ws = _set(db, set_id=13, platform="ielts", track="ielts", title="05.09-06.09")
    pc.sync_weekly_set_event(db, ws, now=NOW)
    ev = _event(db)
    assert ev.title == "IELTS Weekly Test · 05.09-06.09"
    assert ev.meeting_url == "https://ielts.mastereducation.kz/weekly-sets/13"


def test_nuet_track_links_to_the_nuet_host(db, admin, monkeypatch):
    monkeypatch.setenv("NUET_PLATFORM_URL", "https://nuet.example/")
    _group(db, "NUET-A", program="nuet")
    ws = _set(db, set_id=74, track="nuet", title="Weekly Set [NUET] (Week 13)", set_path="/weekly-sets/74")
    pc.sync_weekly_set_event(db, ws, now=NOW)
    assert _event(db).meeting_url == "https://nuet.example/weekly-sets/74"


def test_set_without_a_window_or_already_past_creates_nothing(db, admin):
    _group(db, "SAT-A")
    undated = _set(db, set_id=50, d_from=None, d_to=None)
    past = _set(db, set_id=40, d_from=datetime(2026, 8, 28, 19, 0), d_to=datetime(2026, 8, 30, 18, 59))
    assert pc.sync_weekly_set_event(db, undated, now=NOW)["created"] == 0
    assert pc.sync_weekly_set_event(db, past, now=NOW)["created"] == 0
    assert db.query(Event).count() == 0


def test_without_an_admin_author_nothing_is_created(db):
    _group(db, "SAT-A")
    ws = _set(db)
    assert pc.sync_weekly_set_event(db, ws, now=NOW)["created"] == 0
    assert db.query(Event).count() == 0


def test_set_with_no_target_groups_creates_nothing(db, admin):
    ws = _set(db)
    assert pc.sync_weekly_set_event(db, ws, now=NOW)["created"] == 0


# --- idempotency and updates ----------------------------------------------------------

def test_sync_is_idempotent_and_updated_recomputes_title_window_and_groups(db, admin):
    a = _group(db, "SAT-A")
    b = _group(db, "SAT-B")
    ws = _set(db)
    pc.sync_weekly_set_event(db, ws, now=NOW)
    assert pc.sync_weekly_set_event(db, ws, now=NOW) == {"created": 0, "updated": 1, "deactivated": 0,
                                                          "groups_added": 0, "groups_removed": 0}
    assert db.query(Event).count() == 1

    ws.title = "Weekly Set (05.09-07.09)"
    ws.date_to = datetime(2026, 9, 7, 18, 59, 59)
    b.platform_tests_opt_out = True
    c = _group(db, "SAT-C")
    db.commit()
    out = pc.sync_weekly_set_event(db, ws, now=NOW)
    assert out == {"created": 0, "updated": 1, "deactivated": 0, "groups_added": 1, "groups_removed": 1}
    ev = _event(db)
    assert ev.title.endswith("(05.09-07.09)") and ev.end_datetime == datetime(2026, 9, 7, 18, 59, 59)
    assert _group_ids(db, ev) == [a.id, c.id]


def test_past_set_keeps_its_event_updated_but_never_creates(db, admin):
    _group(db, "SAT-A")
    ws = _set(db)
    pc.sync_weekly_set_event(db, ws, now=NOW)
    ws.title = "Weekly Set (05.09-06.09) edited"
    db.commit()
    later = datetime(2026, 9, 10, 10, 0)
    assert pc.sync_weekly_set_event(db, ws, now=later)["updated"] == 1
    assert _event(db).title.endswith("edited")


def test_unpublished_deactivates_and_republished_reactivates_the_same_event(db, admin):
    a = _group(db, "SAT-A")
    ws = _set(db)
    pc.sync_weekly_set_event(db, ws, now=NOW)
    ws.is_active = False
    db.commit()
    assert pc.sync_weekly_set_event(db, ws, now=NOW)["deactivated"] == 1
    ev = _event(db)
    assert ev.is_active is False and _group_ids(db, ev) == [a.id]     # groups kept for the republish
    ws.is_active = True
    db.commit()
    assert pc.sync_weekly_set_event(db, ws, now=NOW)["updated"] == 1
    assert _event(db).is_active is True and db.query(Event).count() == 1


def test_flag_off_makes_sync_a_noop(db, admin, monkeypatch):
    monkeypatch.setenv("PLATFORM_ASSIGNMENTS_ENABLED", "false")
    _group(db, "SAT-A")
    ws = _set(db)
    assert pc.sync_weekly_set_event(db, ws, now=NOW)["created"] == 0
    assert db.query(Event).count() == 0


def test_sync_all_active_covers_every_platform(db, admin):
    _group(db, "SAT-A")
    _group(db, "IELTS-A", program="ielts")
    _set(db)
    _set(db, set_id=13, platform="ielts", track="ielts", title="05.09-06.09")
    _set(db, set_id=40, d_from=datetime(2026, 8, 28, 19, 0), d_to=datetime(2026, 8, 30, 18, 59))  # past
    out = pc.sync_all_active(db, now=NOW)
    assert out["sets"] == 3 and out["created"] == 2 and db.query(Event).count() == 2
