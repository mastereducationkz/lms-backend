"""HTTP surface of E5 targets: GET/PUT /targets/me for students, GET/PUT /targets/students/{id}
for group staff, admins and (read-only) parents. 503 while PLATFORM_TARGETS_ENABLED is off."""

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
from src.assignments.models import AssignmentZeroSubmission
from src.auth.models import UserInDB
from src.config import get_db
from src.courses.models import Group, GroupStudent
from src.integrations import targets_progress as tp
from src.integrations.models import PlatformResult, PlatformWeeklySet, StudentTarget
from src.integrations.targets_routes import targets_router
from src.parents.models import ParentStudent
from src.routes.auth import get_current_user_dependency


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    return "JSON"


NOW = datetime(2026, 9, 3, 10, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (UserInDB, Group, GroupStudent, AssignmentZeroSubmission, ParentStudent,
                  PlatformResult, PlatformWeeklySet, StudentTarget):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PLATFORM_TARGETS_ENABLED", "true")
    monkeypatch.setattr(tp, "_utcnow", lambda: NOW)
    monkeypatch.setattr(tp, "sat_current", lambda email, **kw: {"total": 1300, "math": 650, "verbal": 650,
                                                                  "week": 5, "set_name": "W5",
                                                                  "completed_at": None, "source": "weekly_set"})


@pytest.fixture()
def world(db):
    def user(email, role):
        u = UserInDB(email=email, name=email.split("@")[0], role=role, hashed_password="h", is_active=True)
        db.add(u)
        db.flush()
        return u

    curator, other_curator = user("cur@x.io", "curator"), user("cur2@x.io", "curator")
    student, parent, admin, outsider = user("stu@x.io", "student"), user("par@x.io", "parent"), user("adm@x.io", "admin"), user("out@x.io", "student")
    group = Group(name="IELTS-A", program_type="ielts", is_active=True, curator_id=curator.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.add(ParentStudent(parent_id=parent.id, student_id=student.id, is_primary=True))
    db.add(PlatformWeeklySet(platform="ielts", weekly_set_id=13, title="13", date_from=datetime(2026, 8, 29, 3, 1),
                             date_to=datetime(2026, 9, 12, 13, 1), is_active=True, track="ielts", modules=[]))
    db.add(PlatformResult(user_id=student.id, platform="ielts", track="ielts", module="listening", attempt_ref="l1",
                          weekly_set_id=13, status="scored", band=6.5, scored_at=datetime(2026, 9, 2)))
    db.commit()
    return {"curator": curator, "other_curator": other_curator, "student": student, "parent": parent,
            "admin": admin, "outsider": outsider, "group": group}


@pytest.fixture()
def make_client(db):
    def _make(user):
        app = FastAPI()
        app.include_router(targets_router, prefix="/targets")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_dependency] = lambda: user
        return TestClient(app)
    return _make


def test_everything_is_503_while_off(make_client, world, monkeypatch):
    monkeypatch.setenv("PLATFORM_TARGETS_ENABLED", "false")
    c = make_client(world["student"])
    assert c.get("/targets/me").status_code == 503
    assert c.put("/targets/me/ielts", json={"targets": {"overall": 7.0}}).status_code == 503
    assert make_client(world["admin"]).get(f"/targets/students/{world['student'].id}").status_code == 503


def test_student_reads_and_sets_own_targets(make_client, world):
    c = make_client(world["student"])
    body = c.get("/targets/me").json()
    assert body["tracks"] == ["ielts"] and body["targets"] == {}
    assert body["progress"]["ielts"]["modules"]["listening"]["now"] == 6.5
    assert body["progress"]["ielts"]["overall_missing"] == ["reading", "writing", "speaking"]

    resp = c.put("/targets/me/ielts", json={"targets": {"overall": 7.5, "listening": 7.0}})
    assert resp.status_code == 200
    assert resp.json()["targets"] == {"overall": 7.5, "listening": 7.0} and resp.json()["source"] == "student"

    body = c.get("/targets/me").json()
    assert body["targets"]["ielts"]["targets"]["overall"] == 7.5
    assert body["progress"]["ielts"]["gaps"]["listening"] == 0.5 and body["progress"]["ielts"]["reached"] is False


def test_invalid_targets_are_400(make_client, world):
    c = make_client(world["student"])
    assert c.put("/targets/me/ielts", json={"targets": {"overall": 7.25}}).status_code == 400
    assert c.put("/targets/me/toefl", json={"targets": {"total": 100}}).status_code == 400
    assert c.put("/targets/me/ielts", json={"nope": 1}).status_code == 422


def test_sat_track_progress_uses_the_weekly_set_current(make_client, world, db):
    g = Group(name="SAT-A", program_type="sat", is_active=True)
    db.add(g)
    db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=world["student"].id))
    db.commit()
    c = make_client(world["student"])
    c.put("/targets/me/sat", json={"targets": {"total": 1400}})
    body = c.get("/targets/me").json()
    assert set(body["tracks"]) == {"ielts", "sat"}
    assert body["progress"]["sat"]["current"]["total"] == 1300 and body["progress"]["sat"]["gaps"]["total"] == 100


def test_non_students_get_403_on_me(make_client, world):
    assert make_client(world["curator"]).get("/targets/me").status_code == 403
    assert make_client(world["parent"]).get("/targets/me").status_code == 403


def test_group_curator_admin_and_parent_can_read_the_student(make_client, world):
    sid = world["student"].id
    for who in ("curator", "admin", "parent"):
        resp = make_client(world[who]).get(f"/targets/students/{sid}")
        assert resp.status_code == 200, who
        assert resp.json()["tracks"] == ["ielts"]
    assert make_client(world["other_curator"]).get(f"/targets/students/{sid}").status_code == 403
    assert make_client(world["outsider"]).get(f"/targets/students/{sid}").status_code == 403


def test_staff_set_targets_with_source_staff_parents_cannot(make_client, world):
    sid = world["student"].id
    resp = make_client(world["curator"]).put(f"/targets/students/{sid}/ielts", json={"targets": {"overall": 8.0}})
    assert resp.status_code == 200 and resp.json()["source"] == "staff" and resp.json()["set_by"] == world["curator"].id
    assert make_client(world["parent"]).put(f"/targets/students/{sid}/ielts", json={"targets": {"overall": 8.0}}).status_code == 403
    assert make_client(world["other_curator"]).put(f"/targets/students/{sid}/ielts", json={"targets": {"overall": 8.0}}).status_code == 403


def test_unknown_student_is_404(make_client, world):
    assert make_client(world["admin"]).get("/targets/students/999").status_code == 404
