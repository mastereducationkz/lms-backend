"""Platform events ingest (Platform Integration Pack §2): envelope validation, idempotency,
user resolution and the projection into platform_results / platform_weekly_sets.

Service-level tests on SQLite; the HTTP surface is covered in test_platform_events_routes.py.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.integrations.ingest import ingest_batch
from src.integrations.models import PlatformEvent, PlatformResult, PlatformWeeklySet


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, PlatformEvent, PlatformResult, PlatformWeeklySet):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db, email="stu@x.io", subject=None, role="student"):
    u = UserInDB(email=email, name="Stu", role=role, hashed_password="h", is_active=True,
                 central_auth_user_id=subject)
    db.add(u)
    db.commit()
    return u


_seq = [0]


def _env(event_type="attempt.submitted", *, event_id=None, student=True, data=None,
         occurred_at="2026-09-03T10:14:05Z", platform="ielts", schema_version=1,
         email="stu@x.io", subject=None):
    _seq[0] += 1
    return {
        "event_id": event_id or f"00000000-0000-4000-8000-{_seq[0]:012d}",
        "event_type": event_type,
        "platform": platform,
        "schema_version": schema_version,
        "occurred_at": occurred_at,
        "student": ({"email": email, "zitadel_subject": subject, "platform_user_id": 828,
                     "platform_student_id": "IELTS-000"} if student else None),
        "data": data if data is not None else {
            "module": "listening", "test_id": 12, "test_title": "Cam 18 T1 L", "attempt_id": 3304,
            "weekly_set_id": 7, "started_at": "2026-09-03T09:40:00Z",
            "finished_at": "2026-09-03T10:14:05Z", "result_url": "/exam/result/3304",
        },
    }


# --- outcomes -------------------------------------------------------------------

def test_valid_event_is_accepted_stored_and_processed(db):
    _user(db)
    env = _env()

    out = ingest_batch(db, "ielts", [env])

    assert out == {"accepted": [env["event_id"]], "duplicates": [], "rejected": []}
    ev = db.query(PlatformEvent).one()
    assert ev.platform == "ielts" and ev.event_type == "attempt.submitted"
    assert ev.email == "stu@x.io" and ev.user_id is not None
    assert ev.payload["data"]["attempt_id"] == 3304
    assert ev.processed_at is not None and ev.error is None
    assert ev.received_at is not None


def test_duplicate_event_id_is_acknowledged_not_reapplied(db):
    _user(db)
    env = _env()
    ingest_batch(db, "ielts", [env])
    env_again = dict(env, data=dict(env["data"], test_title="CHANGED"))

    out = ingest_batch(db, "ielts", [env_again])

    assert out == {"accepted": [], "duplicates": [env["event_id"]], "rejected": []}
    assert db.query(PlatformEvent).count() == 1
    assert db.query(PlatformResult).one().test_title == "Cam 18 T1 L"


def test_same_event_id_on_another_platform_is_a_different_event(db):
    env = _env(platform="ielts")
    ingest_batch(db, "ielts", [env])
    out = ingest_batch(db, "sat", [dict(env, platform="sat")])
    assert out["accepted"] == [env["event_id"]]
    assert db.query(PlatformEvent).count() == 2


@pytest.mark.parametrize("bad, reason", [
    ({"event_id": "not-a-uuid"}, "event_id"),
    ({"occurred_at": "yesterday"}, "occurred_at"),
    ({"schema_version": 2}, "schema_version"),
    ({"platform": "sat"}, "platform"),
    ({"data": "nope"}, "data"),
])
def test_schema_errors_are_rejected_per_event(db, bad, reason):
    good = _env()
    broken = dict(_env(), **bad)

    out = ingest_batch(db, "ielts", [broken, good])

    assert out["accepted"] == [good["event_id"]]
    assert [r["event_id"] for r in out["rejected"]] == [broken["event_id"]]
    assert reason in out["rejected"][0]["reason"]
    assert db.query(PlatformEvent).count() == 1


def test_unknown_event_type_is_stored_unhandled_not_rejected(db):
    env = _env("something.new", data={"x": 1})

    out = ingest_batch(db, "ielts", [env])

    assert out["accepted"] == [env["event_id"]]
    ev = db.query(PlatformEvent).one()
    assert ev.error == "unhandled_event_type" and ev.processed_at is None
    assert db.query(PlatformResult).count() == 0


# --- identity resolution --------------------------------------------------------

def test_zitadel_subject_wins_over_email(db):
    by_subject = _user(db, email="other@x.io", subject="30293")
    _user(db, email="stu@x.io")

    ingest_batch(db, "ielts", [_env(email="stu@x.io", subject="30293")])

    assert db.query(PlatformEvent).one().user_id == by_subject.id


def test_email_match_is_case_insensitive(db):
    u = _user(db, email="Stu@X.io")
    ingest_batch(db, "ielts", [_env(email="stu@x.io")])
    assert db.query(PlatformEvent).one().user_id == u.id


def test_unresolved_student_is_kept_with_error(db):
    ingest_batch(db, "ielts", [_env(email="ghost@x.io")])
    ev = db.query(PlatformEvent).one()
    assert ev.user_id is None and ev.error == "unresolved"
    result = db.query(PlatformResult).one()          # still projected, re-resolved nightly
    assert result.user_id is None


# --- projection: results ---------------------------------------------------------

def test_attempt_lifecycle_projects_one_result_row(db):
    _user(db)
    started = _env("attempt.started", data={"module": "reading", "test_id": 5, "test_title": "R1",
                                             "attempt_id": 90, "weekly_set_id": 7,
                                             "started_at": "2026-09-03T09:00:00Z", "result_url": None},
                   occurred_at="2026-09-03T09:00:00Z")
    submitted = _env("attempt.submitted", data={"module": "reading", "test_id": 5, "test_title": "R1",
                                                 "attempt_id": 90, "weekly_set_id": 7,
                                                 "started_at": "2026-09-03T09:00:00Z",
                                                 "finished_at": "2026-09-03T09:50:00Z",
                                                 "result_url": "/exam/result/90"},
                     occurred_at="2026-09-03T09:50:00Z")
    ready = _env("result.ready", data={"module": "reading", "test_id": 5, "attempt_ref": "90",
                                       "weekly_set_id": 7, "band": 7.5, "raw_score": 33, "total": 40,
                                       "result_url": "/exam/result/90",
                                       "scored_at": "2026-09-03T09:50:01Z"},
                 occurred_at="2026-09-03T09:50:01Z")

    ingest_batch(db, "ielts", [started, submitted, ready])

    r = db.query(PlatformResult).one()
    assert (r.platform, r.module, r.attempt_ref) == ("ielts", "reading", "90")
    assert r.status == "scored" and float(r.band) == 7.5 and r.raw_score == 33 and r.total == 40
    assert r.test_id == 5 and r.test_title == "R1" and r.weekly_set_id == 7 and r.track == "ielts"
    assert r.result_url == "/exam/result/90"
    assert r.started_at == datetime(2026, 9, 3, 9, 0) and r.finished_at == datetime(2026, 9, 3, 9, 50)
    assert r.scored_at == datetime(2026, 9, 3, 9, 50, 1)
    assert r.user_id is not None


def test_late_attempt_event_never_downgrades_a_scored_result(db):
    _user(db)
    ready = _env("result.ready", data={"module": "reading", "test_id": 5, "attempt_ref": "90",
                                       "weekly_set_id": 7, "band": 6.0, "raw_score": 20, "total": 40,
                                       "result_url": "/exam/result/90", "scored_at": "2026-09-03T09:50:01Z"})
    late = _env("attempt.submitted", data={"module": "reading", "test_id": 5, "test_title": "R1",
                                            "attempt_id": 90, "weekly_set_id": 7,
                                            "finished_at": "2026-09-03T09:50:00Z",
                                            "result_url": "/exam/result/90"})
    ingest_batch(db, "ielts", [ready])
    ingest_batch(db, "ielts", [late])
    r = db.query(PlatformResult).one()
    assert r.status == "scored" and float(r.band) == 6.0
    assert r.test_title == "R1"                       # descriptive fields still fill in


@pytest.mark.parametrize("event_type, data, module, ref", [
    ("writing.completed", {"module": "writing", "test_id": 3, "session_id": 555, "weekly_set_id": 7,
                           "finished_at": "2026-09-03T11:00:00Z", "result_url": "/writing/result/555"},
     "writing", "555"),
    ("speaking.completed", {"module": "speaking", "exam_id": 8, "attempt_id": 777, "weekly_set_id": 7,
                            "finished_at": "2026-09-03T11:30:00Z", "result_url": "/speaking-ai/result/777"},
     "speaking", "777"),
    ("attempt.expired", {"module": "listening", "test_id": 1, "test_title": "L", "attempt_id": 42,
                         "weekly_set_id": None, "finished_at": "2026-09-03T12:00:00Z",
                         "result_url": "/exam/result/42"}, "listening", "42"),
])
def test_completion_events_project_by_module_and_ref(db, event_type, data, module, ref):
    _user(db)
    ingest_batch(db, "ielts", [_env(event_type, data=data)])
    r = db.query(PlatformResult).one()
    assert (r.module, r.attempt_ref) == (module, ref)
    assert r.status == ("expired" if event_type == "attempt.expired" else "completed")
    assert r.result_url == data["result_url"]
    assert r.weekly_set_id == data["weekly_set_id"]


# --- projection: weekly sets ------------------------------------------------------

def _set_data(**over):
    base = {"weekly_set_id": 7, "title": "Week 36", "date_from": "2026-09-01", "date_to": "2026-09-07",
            "is_active": True, "track": "ielts",
            "modules": [{"module": "listening", "test_id": 12, "test_title": "Cam 18 T1 L"}]}
    base.update(over)
    return base


def test_weekly_set_published_then_updated_then_unpublished(db):
    ingest_batch(db, "ielts", [_env("weekly_set.published", student=False, data=_set_data())])
    ws = db.query(PlatformWeeklySet).one()
    assert (ws.platform, ws.weekly_set_id, ws.title, ws.is_active, ws.track) == ("ielts", 7, "Week 36", True, "ielts")
    assert str(ws.date_from) == "2026-09-01" and str(ws.date_to) == "2026-09-07"
    assert ws.modules[0]["test_id"] == 12

    ingest_batch(db, "ielts", [_env("weekly_set.updated", student=False, data=_set_data(title="Week 36 (rev)"))])
    assert db.query(PlatformWeeklySet).one().title == "Week 36 (rev)"

    ingest_batch(db, "ielts", [_env("weekly_set.unpublished", student=False,
                                    data={"weekly_set_id": 7, "is_active": False})])
    ws = db.query(PlatformWeeklySet).one()
    assert ws.is_active is False and ws.title == "Week 36 (rev)"


def test_set_level_event_without_student_has_no_identity(db):
    ingest_batch(db, "ielts", [_env("weekly_set.published", student=False, data=_set_data())])
    ev = db.query(PlatformEvent).one()
    assert ev.user_id is None and ev.email is None and ev.error is None
    assert ev.processed_at is not None


def test_events_apply_in_occurred_at_order_within_a_batch(db):
    _user(db)
    later = _env("weekly_set.updated", student=False, data=_set_data(title="second"),
                 occurred_at="2026-09-03T10:00:02Z")
    earlier = _env("weekly_set.published", student=False, data=_set_data(title="first"),
                   occurred_at="2026-09-03T10:00:01Z")
    ingest_batch(db, "ielts", [later, earlier])
    assert db.query(PlatformWeeklySet).one().title == "second"


def test_occurred_at_is_stored_as_naive_utc(db):
    ingest_batch(db, "ielts", [_env(occurred_at="2026-09-03T15:14:05+05:00")])
    assert db.query(PlatformEvent).one().occurred_at == datetime(2026, 9, 3, 10, 14, 5)
