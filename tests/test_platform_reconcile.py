"""Nightly platform job (Platform Integration Pack §2.5): 7-day reconciliation from IELTS's
batch-scores-by-date, re-resolution of unresolved events, 400-day prune."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.integrations import reconcile
from src.integrations.ingest import ingest_batch
from src.integrations.models import PlatformEvent, PlatformResult, PlatformWeeklySet


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    # groups.schedule_config is JSONB; render it as JSON so the table builds on SQLite.
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, Group, GroupStudent, PlatformEvent, PlatformResult, PlatformWeeklySet):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db, email="stu@x.io", subject=None):
    u = UserInDB(email=email, name="Stu", role="student", hashed_password="h", is_active=True,
                 central_auth_user_id=subject)
    db.add(u)
    db.commit()
    return u


def _enrol(db, user, program="ielts", active=True):
    g = Group(name=f"{program}-A", program_type=program, is_active=active)
    db.add(g)
    db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=user.id))
    db.commit()
    return g


def _item(**over):
    base = {
        "email": "stu@x.io",
        "listeningBand": 7.0, "listeningTestName": "L1", "listeningAttemptId": 90,
        "listeningResultUrl": "/exam/result/90", "listeningStatus": "completed",
        "readingBand": None, "readingTestName": "R1", "readingAttemptId": 91,
        "readingResultUrl": None, "readingStatus": "in_progress",
        "writingBand": 6.5, "writingTestName": "W1", "writingSessionId": 555,
        "writingResultUrl": "/writing/result/555", "writingStatus": "completed",
        "speakingBand": None, "speakingTestName": None, "speakingAttemptId": None,
        "speakingResultUrl": None, "speakingStatus": None,
        "overallBand": None,
    }
    base.update(over)
    return base


def _payload(items=None, set_id=7):
    return {"weeklySetId": set_id, "weeklySetDateFrom": "2026-09-01", "weeklySetDateTo": "2026-09-07",
            "weeklySetTitle": "Week 36", "results": items if items is not None else [_item()]}


class _Fetch:
    """Stand-in for IELTSService.fetch_batch_scores_by_date: records calls, answers per date."""

    def __init__(self, answers):
        self.answers = answers      # date_str -> payload
        self.calls = []

    def __call__(self, emails, date_str):
        self.calls.append((sorted(emails), date_str))
        return self.answers.get(date_str, {"results": []})


# --- who gets reconciled ---------------------------------------------------------

def test_only_students_in_active_ielts_groups_are_reconciled(db):
    u_ielts = _user(db, "a@x.io")
    _enrol(db, u_ielts, "ielts")
    u_sat = _user(db, "b@x.io")
    _enrol(db, u_sat, "sat")
    u_inactive = _user(db, "c@x.io")
    _enrol(db, u_inactive, "ielts", active=False)

    assert reconcile.ielts_track_emails(db) == ["a@x.io"]


def test_reconcile_calls_each_of_the_last_7_days_and_dedupes_sets(db):
    _enrol(db, _user(db), "ielts")
    fetch = _Fetch({d: _payload() for d in ("2026-09-03", "2026-09-02", "2026-09-01")})

    out = reconcile.reconcile_ielts(db, today=date(2026, 9, 3), fetch=fetch)

    assert [c[1] for c in fetch.calls] == [f"2026-09-{d:02d}" for d in range(3, 0, -1)] + [
        "2026-08-31", "2026-08-30", "2026-08-29", "2026-08-28"]
    assert out["weekly_sets"] == 1                     # set 7 seen on 3 days, applied once
    assert db.query(PlatformResult).count() == 3       # listening, reading, writing (no speaking ref)


def test_reconciled_rows_carry_real_refs_status_and_band(db):
    u = _user(db)
    _enrol(db, u, "ielts")
    fetch = _Fetch({"2026-09-03": _payload()})

    reconcile.reconcile_ielts(db, today=date(2026, 9, 3), fetch=fetch)

    rows = {r.module: r for r in db.query(PlatformResult).all()}
    assert set(rows) == {"listening", "reading", "writing"}
    listening = rows["listening"]
    assert (listening.attempt_ref, listening.status, listening.band) == ("90", "scored", 7.0)
    assert listening.test_title == "L1" and listening.result_url == "/exam/result/90"
    assert listening.weekly_set_id == 7 and listening.user_id == u.id and listening.track == "ielts"
    assert (rows["reading"].attempt_ref, rows["reading"].status, rows["reading"].band) == ("91", "started", None)
    assert (rows["writing"].attempt_ref, rows["writing"].status, rows["writing"].band) == ("555", "scored", 6.5)
    ws = db.query(PlatformWeeklySet).one()
    assert (ws.weekly_set_id, ws.title, str(ws.date_from), str(ws.date_to)) == (7, "Week 36", "2026-09-01", "2026-09-07")


def test_reconcile_never_downgrades_an_event_sourced_row(db):
    _user(db)
    _enrol(db, db.query(UserInDB).one(), "ielts")
    ingest_batch(db, "ielts", [{
        "event_id": "00000000-0000-4000-8000-000000000001", "event_type": "result.ready", "platform": "ielts",
        "schema_version": 1, "occurred_at": "2026-09-03T09:50:01Z",
        "student": {"email": "stu@x.io", "zitadel_subject": None},
        "data": {"module": "listening", "test_id": 12, "attempt_ref": "90", "weekly_set_id": 7, "band": 7.5,
                 "raw_score": 35, "total": 40, "result_url": "/exam/result/90", "scored_at": "2026-09-03T09:50:01Z"},
    }])
    fetch = _Fetch({"2026-09-03": _payload([_item(listeningStatus="in_progress", listeningBand=None)])})

    reconcile.reconcile_ielts(db, today=date(2026, 9, 3), fetch=fetch)

    listening = db.query(PlatformResult).filter_by(module="listening").one()
    assert listening.status == "scored" and listening.band == 7.5 and listening.raw_score == 35


def test_reconcile_fills_a_missing_band_on_a_completed_row(db):
    _enrol(db, _user(db), "ielts")
    first = _Fetch({"2026-09-03": _payload([_item(listeningBand=None, listeningStatus="completed")])})
    reconcile.reconcile_ielts(db, today=date(2026, 9, 3), fetch=first)
    assert db.query(PlatformResult).filter_by(module="listening").one().status == "completed"

    second = _Fetch({"2026-09-03": _payload([_item(listeningBand=8.0)])})
    reconcile.reconcile_ielts(db, today=date(2026, 9, 3), fetch=second)
    row = db.query(PlatformResult).filter_by(module="listening").one()
    assert row.status == "scored" and row.band == 8.0


def test_reconcile_tolerates_a_failed_day(db):
    _enrol(db, _user(db), "ielts")

    def fetch(emails, date_str):
        if date_str == "2026-09-02":
            raise RuntimeError("ielts down")
        return _payload() if date_str == "2026-09-03" else {"results": []}

    out = reconcile.reconcile_ielts(db, today=date(2026, 9, 3), fetch=fetch)
    assert out["errors"] == 1 and db.query(PlatformResult).count() == 3


def test_reconcile_without_ielts_students_makes_no_calls(db):
    fetch = _Fetch({})
    out = reconcile.reconcile_ielts(db, today=date(2026, 9, 3), fetch=fetch)
    assert fetch.calls == [] and out["students"] == 0


# --- re-resolution --------------------------------------------------------------

def test_unresolved_events_are_re_resolved_when_the_user_appears(db):
    ingest_batch(db, "ielts", [{
        "event_id": "00000000-0000-4000-8000-000000000002", "event_type": "attempt.submitted", "platform": "ielts",
        "schema_version": 1, "occurred_at": "2026-09-03T10:00:00Z",
        "student": {"email": "late@x.io", "zitadel_subject": "777"},
        "data": {"module": "reading", "test_id": 5, "test_title": "R1", "attempt_id": 90, "weekly_set_id": 7,
                 "finished_at": "2026-09-03T10:00:00Z", "result_url": "/exam/result/90"},
    }])
    assert db.query(PlatformEvent).one().error == "unresolved"
    assert db.query(PlatformResult).one().user_id is None

    u = _user(db, email="other@x.io", subject="777")     # linked by subject, different email
    fixed = reconcile.re_resolve_unresolved(db)

    assert fixed == 1
    ev = db.query(PlatformEvent).one()
    assert ev.user_id == u.id and ev.error is None
    assert db.query(PlatformResult).one().user_id == u.id


def test_re_resolution_leaves_still_unknown_students_alone(db):
    ingest_batch(db, "ielts", [{
        "event_id": "00000000-0000-4000-8000-000000000003", "event_type": "attempt.started", "platform": "ielts",
        "schema_version": 1, "occurred_at": "2026-09-03T10:00:00Z",
        "student": {"email": "ghost@x.io", "zitadel_subject": None},
        "data": {"module": "reading", "test_id": 5, "attempt_id": 1, "weekly_set_id": 7},
    }])
    assert reconcile.re_resolve_unresolved(db) == 0
    assert db.query(PlatformEvent).one().error == "unresolved"


# --- prune ----------------------------------------------------------------------

def test_prune_deletes_events_older_than_400_days(db):
    now = datetime(2026, 9, 3, 3, 30)
    old = PlatformEvent(platform="ielts", event_id="old", event_type="x", occurred_at=now - timedelta(days=401),
                        received_at=now - timedelta(days=401), payload={})
    young = PlatformEvent(platform="ielts", event_id="young", event_type="x", occurred_at=now - timedelta(days=399),
                          received_at=now - timedelta(days=399), payload={})
    db.add_all([old, young])
    db.commit()

    assert reconcile.prune_events(db, now=now) == 1
    assert [e.event_id for e in db.query(PlatformEvent).all()] == ["young"]


# --- scheduling -------------------------------------------------------------------

def test_nightly_is_due_once_per_day_at_0330_almaty():
    almaty_0329 = datetime(2026, 9, 2, 22, 29)   # UTC == 03:29 Asia/Almaty (UTC+5)
    almaty_0331 = datetime(2026, 9, 2, 22, 31)
    assert reconcile.nightly_due(almaty_0329, last_run_date=None) is False
    assert reconcile.nightly_due(almaty_0331, last_run_date=None) is True
    assert reconcile.nightly_due(almaty_0331, last_run_date=date(2026, 9, 3)) is False   # already ran today
    assert reconcile.nightly_due(datetime(2026, 9, 3, 22, 31), last_run_date=date(2026, 9, 3)) is True
