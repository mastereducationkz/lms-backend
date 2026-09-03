"""Per-student progress on platform-test assignments (E1 checkmarks, E2 countdown data):
module states from platform_results, the auto-submission when every module is done, the
student's weekly-tests feed and the staff matrix."""

import json
from datetime import datetime

import pytest
from sqlalchemy import ARRAY, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.assignments.models import Assignment, AssignmentSubmission
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.integrations import platform_assignments as pa
from src.integrations import platform_progress as pp
from src.integrations.ingest import ingest_batch
from src.integrations.models import (
    PlatformEvent, PlatformResult, PlatformTestAssignment, PlatformWeeklySet,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    # assignments.allowed_file_types is a Postgres ARRAY; unused here.
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, Group, GroupStudent, Assignment, AssignmentSubmission,
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
    monkeypatch.setattr(pa, "_utcnow", lambda: NOW)
    monkeypatch.setattr(pp, "_utcnow", lambda: NOW)


NOW = datetime(2026, 9, 3, 10, 0)                  # UTC, inside set 13's window
D_FROM, D_TO = datetime(2026, 8, 29, 3, 1), datetime(2026, 9, 12, 13, 1)
MODULES = [
    {"module": "listening", "test_id": 41, "test_title": "L 41"},
    {"module": "reading", "test_id": 42, "test_title": "R 42"},
    {"module": "writing", "test_id": 7, "test_title": "W 7"},
    {"module": "speaking", "test_id": 9, "test_title": "S 9"},
]


def _user(db, email="stu@x.io"):
    u = UserInDB(email=email, name=email.split("@")[0], role="student", hashed_password="h", is_active=True)
    db.add(u)
    db.commit()
    return u


def _group(db, name="IELTS-A"):
    g = Group(name=name, program_type="ielts", is_active=True)
    db.add(g)
    db.commit()
    return g


def _enrol(db, g, u):
    db.add(GroupStudent(group_id=g.id, student_id=u.id))
    db.commit()


def _set(db, set_id=13, title="29.08-30.08", d_from=D_FROM, d_to=D_TO, modules=None):
    ws = PlatformWeeklySet(platform="ielts", weekly_set_id=set_id, title=title, date_from=d_from, date_to=d_to,
                           is_active=True, track="ielts", modules=MODULES if modules is None else modules)
    db.add(ws)
    db.commit()
    return ws


def _result(db, user_id, module, status, ref, set_id=13, band=None, url=None, finished=None):
    db.add(PlatformResult(user_id=user_id, platform="ielts", track="ielts", module=module, attempt_ref=ref,
                          weekly_set_id=set_id, status=status, band=band, result_url=url, finished_at=finished))
    db.commit()


def _assignment(db):
    return db.query(Assignment).one()


# --- module states ---------------------------------------------------------------

def test_module_states_from_results(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db), now=NOW)
    _result(db, u.id, "listening", "scored", "90", band=7.5, url="/exam/result/90")
    _result(db, u.id, "reading", "started", "91")

    states = pp.module_states(db, json.loads(_assignment(db).content), u.id, now=NOW)

    by = {s["module"]: s for s in states}
    assert by["listening"]["state"] == "done" and by["listening"]["band"] == 7.5
    assert by["listening"]["result_url"] == "/exam/result/90" and by["listening"]["path"] == "/exam/test/41"
    assert by["reading"]["state"] == "in_progress"
    assert by["writing"]["state"] == "not_started" and by["writing"]["path"] == "/weekly-sets/13"
    assert by["listening"]["deadline_kind"] == "due" and by["speaking"]["deadline_kind"] == "closes"
    assert pp.summarize(states) == "in_progress"


def test_expired_counts_as_done_and_lrw_stay_available_after_date_to(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db), now=NOW)
    _result(db, u.id, "listening", "expired", "90")

    states = pp.module_states(db, json.loads(_assignment(db).content), u.id, now=datetime(2026, 9, 20, 9, 0))

    by = {s["module"]: s for s in states}
    assert by["listening"]["state"] == "done"
    assert by["reading"]["available"] is True and by["writing"]["available"] is True
    assert by["speaking"]["available"] is False


def test_speaking_available_only_inside_the_exact_window(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db), now=NOW)
    content = json.loads(_assignment(db).content)

    def speaking(now):
        return {s["module"]: s for s in pp.module_states(db, content, u.id, now=now)}["speaking"]["available"]

    assert speaking(datetime(2026, 8, 29, 3, 0)) is False
    assert speaking(datetime(2026, 8, 29, 3, 1)) is True
    assert speaking(datetime(2026, 9, 12, 13, 1)) is True
    assert speaking(datetime(2026, 9, 12, 13, 2)) is False


def test_latest_result_row_wins_per_module(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db), now=NOW)
    _result(db, u.id, "listening", "started", "1")
    _result(db, u.id, "listening", "scored", "2", band=6.0)
    by = {s["module"]: s for s in pp.module_states(db, json.loads(_assignment(db).content), u.id, now=NOW)}
    assert by["listening"]["state"] == "done" and by["listening"]["band"] == 6.0


# --- auto-submission ----------------------------------------------------------------

def test_all_done_writes_one_auto_submission_that_grading_queues_ignore(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db), now=NOW)
    a = _assignment(db)
    for module, status, ref in (("listening", "scored", "1"), ("reading", "submitted", "2"), ("writing", "completed", "3")):
        _result(db, u.id, module, status, ref, finished=datetime(2026, 9, 3, 9, 0))
    assert pp.on_result_change(db, "ielts", 13, u.id) == 0         # speaking still missing

    _result(db, u.id, "speaking", "completed", "4", finished=datetime(2026, 9, 3, 9, 30))
    assert pp.on_result_change(db, "ielts", 13, u.id) == 1

    sub = db.query(AssignmentSubmission).one()
    assert (sub.assignment_id, sub.user_id, sub.is_graded, sub.score) == (a.id, u.id, True, None)
    assert sub.submitted_at == datetime(2026, 9, 3, 9, 30) and sub.is_late is False
    assert json.loads(sub.answers)["modules"][0]["module"] == "listening"
    assert pp.on_result_change(db, "ielts", 13, u.id) == 0         # idempotent
    assert db.query(AssignmentSubmission).count() == 1


def test_submitted_requires_only_the_modules_the_set_contains(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db, modules=MODULES[:3]), now=NOW)   # no Speaking this week
    for module, status, ref in (("listening", "scored", "1"), ("reading", "expired", "2"), ("writing", "scored", "3")):
        _result(db, u.id, module, status, ref)
    assert pp.on_result_change(db, "ielts", 13, u.id) == 1


def test_ingesting_the_last_module_event_creates_the_submission(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db, modules=MODULES[:1]), now=NOW)
    ingest_batch(db, "ielts", [{
        "event_id": "00000000-0000-4000-8000-000000000501", "event_type": "attempt.submitted", "platform": "ielts",
        "schema_version": 1, "occurred_at": "2026-09-03T10:00:00Z",
        "student": {"email": "stu@x.io", "zitadel_subject": None},
        "data": {"module": "listening", "test_id": 41, "attempt_id": 900, "weekly_set_id": 13,
                 "finished_at": "2026-09-03T10:00:00Z", "result_url": "/exam/result/900"},
    }])
    assert db.query(AssignmentSubmission).count() == 1


def test_flag_off_writes_nothing(db, monkeypatch):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db, modules=MODULES[:1]), now=NOW)
    monkeypatch.setenv("PLATFORM_ASSIGNMENTS_ENABLED", "false")
    _result(db, u.id, "listening", "scored", "1")
    assert pp.on_result_change(db, "ielts", 13, u.id) == 0
    assert db.query(AssignmentSubmission).count() == 0


# --- feed + matrix --------------------------------------------------------------------

def test_weekly_tests_for_student_lists_current_then_past(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    _set(db, set_id=12, title="22.08-23.08", d_from=datetime(2026, 8, 22, 3, 1), d_to=datetime(2026, 8, 29, 13, 1))
    _set(db)
    pa.sync_all_active(db, now=NOW, include_past=True)

    items = pp.weekly_tests_for_student(db, u.id, now=NOW)

    assert [i["weekly_set_id"] for i in items] == [13, 12]
    current = items[0]
    assert current["status"] == "not_started" and current["days_left"] == 9
    assert current["due_date"] == "2026-09-12T13:01:00+00:00" and current["set_path"] == "/weekly-sets/13"
    assert {m["module"]: m["deadline_kind"] for m in current["modules"]}["speaking"] == "closes"
    assert items[1]["days_left"] == -5


def test_student_progress_shape(db):
    u = _user(db); g = _group(db); _enrol(db, g, u)
    pa.sync_weekly_set(db, _set(db), now=NOW)
    p = pp.student_progress(db, _assignment(db), u.id, now=NOW)
    assert p["assignment_id"] == _assignment(db).id and p["group_id"] == g.id
    assert p["platform"] == "ielts" and p["weekly_set_id"] == 13 and p["title"].startswith("IELTS Weekly Test")
    assert p["date_from"] == "2026-08-29T03:01:00+00:00" and p["status"] == "not_started"
    assert len(p["modules"]) == 4


def test_group_matrix_lists_every_student(db):
    g = _group(db); u1 = _user(db, "a@x.io"); u2 = _user(db, "b@x.io"); _enrol(db, g, u1); _enrol(db, g, u2)
    pa.sync_weekly_set(db, _set(db), now=NOW)
    _result(db, u1.id, "listening", "scored", "1", band=6.0)

    rows = pp.group_matrix(db, _assignment(db), now=NOW)

    assert [r["user_id"] for r in rows] == [u1.id, u2.id]
    assert rows[0]["modules"][0]["state"] == "done" and rows[0]["status"] == "in_progress"
    assert rows[1]["status"] == "not_started" and rows[1]["name"] == "b"
