"""IELTS diagnostic entry bands (Platform Integration Pack §2.6): fetched nightly in batches of
≤500, stored per student, surfaced as the "start" segment of the targets tile — never fetched on
a student request."""

from datetime import datetime

import pytest
from sqlalchemy import ARRAY, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.assignments.models import AssignmentZeroSubmission
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.integrations import diagnostics as dg
from src.integrations import targets_progress as tp
from src.integrations.models import PlatformDiagnostic, PlatformResult, PlatformWeeklySet


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    return "JSON"


NOW = datetime(2026, 9, 3, 10, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, Group, GroupStudent, AssignmentZeroSubmission, PlatformResult, PlatformWeeklySet,
                  PlatformDiagnostic):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _student(db, email, subject=None, program="ielts"):
    u = UserInDB(email=email, name=email.split("@")[0], role="student", hashed_password="h", is_active=True,
                 central_auth_user_id=subject)
    db.add(u)
    db.flush()
    g = Group(name=f"{program}-{email}", program_type=program, is_active=True)
    db.add(g)
    db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=u.id))
    db.commit()
    return u


DIAG = {
    "listening": {"band": 5.5, "completedAt": "2026-06-01T10:40:00+00:00", "resultUrl": "/exam/result/123"},
    "reading": {"band": 6.0, "completedAt": "2026-06-01T11:40:00+00:00", "resultUrl": "/exam/result/124"},
    "writing": {"band": None, "completedAt": "2026-06-02T09:00:00+00:00", "resultUrl": "/writing/result/456"},
    "completedCount": 3,
    "overallBand": None,
}


class _Fetch:
    def __init__(self, answers):
        self.answers = answers      # email -> diagnostic dict | None | "missing"
        self.calls = []

    def __call__(self, students):
        self.calls.append(students)
        results = []
        for s in students:
            email = s["email"]
            answer = self.answers.get(email, "missing")
            results.append({"email": email, "found": answer != "missing",
                            "diagnostic": None if answer in ("missing", None) else answer})
        return {"results": results}


# --- normalisation ---------------------------------------------------------------------

def test_normalise_diagnostic_keeps_taken_but_unscored_modules():
    out = dg.normalise(DIAG)
    assert out["listening"] == {"band": 5.5, "completed_at": "2026-06-01T10:40:00+00:00", "result_url": "/exam/result/123"}
    assert out["writing"] == {"band": None, "completed_at": "2026-06-02T09:00:00+00:00", "result_url": "/writing/result/456"}
    assert out["completed_count"] == 3 and out["overall"] is None


def test_normalise_none_is_none():
    assert dg.normalise(None) is None


# --- refresh ---------------------------------------------------------------------------

def test_refresh_stores_one_row_per_ielts_student_and_batches_by_500(db):
    a = _student(db, "a@x.io", subject="111")
    b = _student(db, "b@x.io")
    _student(db, "c@x.io", program="sat")                     # not IELTS-track: not asked
    fetch = _Fetch({"a@x.io": DIAG, "b@x.io": None})

    out = dg.refresh_diagnostics(db, fetch=fetch, now=NOW)

    assert out == {"students": 2, "batches": 1, "stored": 1, "none": 1, "errors": 0}
    assert fetch.calls == [[{"email": "a@x.io", "central_auth_user_id": "111"}, {"email": "b@x.io"}]]
    rows = {r.user_id: r for r in db.query(PlatformDiagnostic).all()}
    assert set(rows) == {a.id, b.id}
    assert rows[a.id].payload["listening"]["band"] == 5.5 and rows[a.id].fetched_at == NOW
    assert rows[b.id].payload is None                        # asked, never taken: remembered as none


def test_refresh_is_idempotent_and_updates_in_place(db):
    a = _student(db, "a@x.io")
    dg.refresh_diagnostics(db, fetch=_Fetch({"a@x.io": None}), now=NOW)
    later = {**DIAG, "writing": {"band": 6.0, "completedAt": "2026-06-02T09:00:00+00:00", "resultUrl": "/writing/result/456"},
             "overallBand": 6.0}
    dg.refresh_diagnostics(db, fetch=_Fetch({"a@x.io": later}), now=datetime(2026, 9, 4, 10, 0))
    row = db.query(PlatformDiagnostic).one()
    assert row.user_id == a.id and row.payload["overall"] == 6.0 and row.fetched_at == datetime(2026, 9, 4, 10, 0)


def test_refresh_batches_of_500(db):
    for i in range(501):
        _student(db, f"s{i}@x.io")
    fetch = _Fetch({})
    out = dg.refresh_diagnostics(db, fetch=fetch, now=NOW)
    assert out["batches"] == 2 and [len(c) for c in fetch.calls] == [500, 1]
    assert out["students"] == 501 and out["none"] == 501


def test_refresh_survives_a_failed_batch(db):
    _student(db, "a@x.io")

    def boom(students):
        raise RuntimeError("ielts down")

    out = dg.refresh_diagnostics(db, fetch=boom, now=NOW)
    assert out["errors"] == 1 and db.query(PlatformDiagnostic).count() == 0


def test_flag_off_makes_refresh_a_noop(db, monkeypatch):
    monkeypatch.setenv("PLATFORM_TARGETS_ENABLED", "false")
    _student(db, "a@x.io")
    fetch = _Fetch({"a@x.io": DIAG})
    assert dg.refresh_diagnostics(db, fetch=fetch, now=NOW)["students"] == 0 and fetch.calls == []


# --- the start segment on the tile -----------------------------------------------------

@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("PLATFORM_TARGETS_ENABLED", "true")


def test_progress_carries_the_stored_start_segment(db):
    a = _student(db, "a@x.io")
    dg.refresh_diagnostics(db, fetch=_Fetch({"a@x.io": DIAG}), now=NOW)
    p = tp.ielts_progress(db, a.id, now=NOW)
    assert p["start"]["listening"]["band"] == 5.5 and p["start"]["writing"]["band"] is None
    assert p["start"]["completed_count"] == 3 and p["start"]["overall"] is None
    assert p["start"]["fetched_at"] == "2026-09-03T10:00:00+00:00"


def test_progress_start_is_none_without_a_diagnostic(db):
    a = _student(db, "a@x.io")
    assert tp.ielts_progress(db, a.id, now=NOW)["start"] is None
    dg.refresh_diagnostics(db, fetch=_Fetch({"a@x.io": None}), now=NOW)
    assert tp.ielts_progress(db, a.id, now=NOW)["start"] is None
