"""HTTP surface of E1/E2: the student's weekly-tests feed, per-assignment platform progress
(student view / staff matrix), the per-group opt-out and the admin sync. All 503 while
PLATFORM_ASSIGNMENTS_ENABLED is off."""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import ARRAY, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.schemas.models  # noqa: F401 - register models
from src.assignments.models import Assignment, AssignmentSubmission
from src.auth.models import UserInDB
from src.events.models import Event, EventGroup
from src.integrations.models import PlatformTestEvent
from src.config import get_db
from src.courses.models import Group, GroupStudent
from src.integrations import platform_assignments as pa
from src.integrations import platform_progress as pp
from src.integrations.assignment_routes import platform_assignments_router
from src.integrations.models import (
    PlatformEvent, PlatformResult, PlatformTestAssignment, PlatformWeeklySet,
)
from src.routes.auth import get_current_user_dependency


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    return "JSON"


NOW = datetime(2026, 9, 3, 10, 0)
MODULES = [{"module": "listening", "test_id": 41, "test_title": "L 41"},
           {"module": "speaking", "test_id": 9, "test_title": "S 9"}]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Event, EventGroup, PlatformTestEvent, UserInDB, Group, GroupStudent, Assignment, AssignmentSubmission,
                  PlatformEvent, PlatformResult, PlatformWeeklySet, PlatformTestAssignment):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_ASSIGNMENTS_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_TEST_HOMEWORK", "true")
    monkeypatch.setattr(pa, "_utcnow", lambda: NOW)
    # platform_progress keeps its own _utcnow, and that is the one the HTTP routes reach for
    # when they derive days_left. Leaving it on the wall clock made this module a time bomb:
    # the fixed `date_to` drifts one day closer every day, so the suite passed when written
    # and went red on its own two days later. Freeze both clocks, as the fixture intended.
    monkeypatch.setattr(pp, "_utcnow", lambda: NOW)


@pytest.fixture()
def world(db):
    """A curator, two students (one in the IELTS group, one outside), a parent, an admin, and
    one platform_test assignment for the group."""
    def user(email, role):
        u = UserInDB(email=email, name=email.split("@")[0], role=role, hashed_password="h", is_active=True)
        db.add(u)
        db.flush()
        return u

    curator, other_curator = user("cur@x.io", "curator"), user("cur2@x.io", "curator")
    student, outsider = user("stu@x.io", "student"), user("out@x.io", "student")
    parent, admin = user("par@x.io", "parent"), user("adm@x.io", "admin")
    group = Group(name="IELTS-A", program_type="ielts", is_active=True, curator_id=curator.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    ws = PlatformWeeklySet(platform="ielts", weekly_set_id=13, title="29.08-30.08",
                           date_from=datetime(2026, 8, 29, 3, 1), date_to=datetime(2026, 9, 12, 13, 1),
                           is_active=True, track="ielts", modules=MODULES)
    db.add(ws)
    db.commit()
    pa.sync_weekly_set(db, ws, now=NOW)
    assignment = db.query(Assignment).one()
    return {"curator": curator, "other_curator": other_curator, "student": student, "outsider": outsider,
            "parent": parent, "admin": admin, "group": group, "assignment": assignment, "ws": ws}


@pytest.fixture()
def make_client(db):
    def _make(user):
        app = FastAPI()
        app.include_router(platform_assignments_router, prefix="/integrations")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_dependency] = lambda: user
        return TestClient(app)
    return _make


# --- flag ----------------------------------------------------------------------

def test_everything_is_503_while_the_flag_is_off(make_client, world, monkeypatch):
    monkeypatch.setenv("PLATFORM_ASSIGNMENTS_ENABLED", "false")
    c = make_client(world["admin"])
    aid, gid = world["assignment"].id, world["group"].id
    assert c.get("/integrations/weekly-tests/me").status_code == 503
    assert c.get(f"/integrations/assignments/{aid}/platform-progress").status_code == 503
    assert c.patch(f"/integrations/groups/{gid}/platform-tests", json={"opt_out": True}).status_code == 503
    assert c.post("/integrations/platform-tests/sync").status_code == 503


# --- weekly tests feed -----------------------------------------------------------

def test_student_feed_lists_the_open_test(make_client, world):
    resp = make_client(world["student"]).get("/integrations/weekly-tests/me")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1 and items[0]["weekly_set_id"] == 13 and items[0]["status"] == "not_started"
    assert items[0]["days_left"] == 9 and [m["module"] for m in items[0]["modules"]] == ["listening", "speaking"]


def test_feed_is_empty_for_staff_and_outsiders(make_client, world):
    assert make_client(world["curator"]).get("/integrations/weekly-tests/me").json() == {"items": []}
    assert make_client(world["outsider"]).get("/integrations/weekly-tests/me").json() == {"items": []}


# --- platform progress -------------------------------------------------------------

def test_student_sees_own_progress(make_client, world, db):
    db.add(PlatformResult(user_id=world["student"].id, platform="ielts", track="ielts", module="listening",
                          attempt_ref="90", weekly_set_id=13, status="scored", band=7.0, result_url="/exam/result/90"))
    db.commit()
    resp = make_client(world["student"]).get(f"/integrations/assignments/{world['assignment'].id}/platform-progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "in_progress" and body["modules"][0]["state"] == "done" and body["modules"][0]["band"] == 7.0
    assert "students" not in body


def test_student_outside_the_group_is_403(make_client, world):
    resp = make_client(world["outsider"]).get(f"/integrations/assignments/{world['assignment'].id}/platform-progress")
    assert resp.status_code == 403


def test_group_curator_and_admin_get_the_matrix_other_curator_403(make_client, world):
    aid = world["assignment"].id
    for who in ("curator", "admin"):
        body = make_client(world[who]).get(f"/integrations/assignments/{aid}/platform-progress").json()
        assert body["assignment"]["weekly_set_id"] == 13
        assert [s["email"] for s in body["students"]] == ["stu@x.io"]
    assert make_client(world["other_curator"]).get(f"/integrations/assignments/{aid}/platform-progress").status_code == 403


def test_progress_404_for_a_non_platform_assignment(make_client, world, db):
    plain = Assignment(title="essay", assignment_type="essay", content="{}", group_id=world["group"].id)
    db.add(plain)
    db.commit()
    assert make_client(world["admin"]).get(f"/integrations/assignments/{plain.id}/platform-progress").status_code == 404


# --- opt-out -------------------------------------------------------------------------

def test_group_curator_can_opt_out_and_back_in(make_client, world, db):
    gid = world["group"].id
    c = make_client(world["curator"])
    resp = c.patch(f"/integrations/groups/{gid}/platform-tests", json={"opt_out": True})
    assert resp.status_code == 200 and resp.json()["opt_out"] is True
    db.expire_all()
    assert db.query(Assignment).one().is_active is False
    resp = c.patch(f"/integrations/groups/{gid}/platform-tests", json={"opt_out": False})
    assert resp.status_code == 200 and resp.json()["opt_out"] is False
    db.expire_all()
    assert db.query(Assignment).one().is_active is True


def test_opt_out_forbidden_for_students_parents_and_other_curators(make_client, world):
    gid = world["group"].id
    for who in ("student", "parent", "other_curator"):
        resp = make_client(world[who]).patch(f"/integrations/groups/{gid}/platform-tests", json={"opt_out": True})
        assert resp.status_code == 403, who


def test_opt_out_unknown_group_is_404(make_client, world):
    assert make_client(world["admin"]).patch("/integrations/groups/999/platform-tests", json={"opt_out": True}).status_code == 404


# --- admin sync ----------------------------------------------------------------------

def test_admin_sync_reports_counts_and_others_are_403(make_client, world, db):
    db.add(Group(name="IELTS-B", program_type="ielts", is_active=True))
    db.commit()
    resp = make_client(world["admin"]).post("/integrations/platform-tests/sync")
    assert resp.status_code == 200 and resp.json()["created"] == 1 and resp.json()["sets"] == 1
    assert make_client(world["curator"]).post("/integrations/platform-tests/sync").status_code == 403
