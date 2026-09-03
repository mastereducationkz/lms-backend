"""HTTP contract of ``POST /integrations/events`` (Platform Integration Pack §2.1):
per-platform X-API-Key, X-Platform header, 503 while the ingest flag is off, ≤100 per batch,
per-event outcomes in the 200 body."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.config import get_db
from src.integrations.models import PlatformEvent, PlatformResult, PlatformWeeklySet
from src.integrations.routes import router

IELTS_KEY = "ielts-events-key"
SAT_KEY = "sat-events-key"


@pytest.fixture()
def db():
    # One shared in-memory connection: TestClient serves requests on another thread.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (UserInDB, PlatformEvent, PlatformResult, PlatformWeeklySet):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("IELTS_EVENTS_API_KEY", IELTS_KEY)
    monkeypatch.setenv("SAT_EVENTS_API_KEY", SAT_KEY)
    monkeypatch.setenv("PLATFORM_EVENTS_INGEST_ENABLED", "true")
    app = FastAPI()
    app.include_router(router, prefix="/integrations")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _event(n=1, platform="ielts"):
    return {
        "event_id": f"00000000-0000-4000-8000-{n:012d}",
        "event_type": "weekly_set.published",
        "platform": platform,
        "schema_version": 1,
        "occurred_at": "2026-09-03T10:00:00Z",
        "student": None,
        "data": {"weekly_set_id": n, "title": f"Week {n}", "date_from": "2026-09-01",
                 "date_to": "2026-09-07", "is_active": True, "track": "ielts", "modules": []},
    }


def _post(client, events, *, key=IELTS_KEY, platform="ielts"):
    headers = {"X-Platform": platform}
    if key is not None:
        headers["X-API-Key"] = key
    return client.post("/integrations/events", json={"events": events}, headers=headers)


# --- auth ----------------------------------------------------------------------

def test_missing_key_is_401(client):
    assert _post(client, [_event()], key=None).status_code == 401


def test_wrong_key_is_401(client):
    assert _post(client, [_event()], key="nope").status_code == 401


def test_key_is_bound_to_its_platform(client):
    # IELTS's key must not authenticate a batch claiming to be SAT (and vice versa).
    assert _post(client, [_event(platform="sat")], key=IELTS_KEY, platform="sat").status_code == 401
    assert _post(client, [_event(platform="sat")], key=SAT_KEY, platform="sat").status_code == 200


def test_unknown_platform_header_is_401(client):
    assert _post(client, [_event()], platform="toefl").status_code == 401


def test_unconfigured_key_never_matches_empty(client, monkeypatch):
    monkeypatch.delenv("SAT_EVENTS_API_KEY", raising=False)
    assert _post(client, [_event(platform="sat")], key="", platform="sat").status_code == 401


# --- flag ----------------------------------------------------------------------

def test_flag_off_is_503_after_auth(client, monkeypatch, db):
    monkeypatch.setenv("PLATFORM_EVENTS_INGEST_ENABLED", "false")
    assert _post(client, [_event()]).status_code == 503
    assert _post(client, [_event()], key="nope").status_code == 401   # auth still checked first
    assert db.query(PlatformEvent).count() == 0


# --- batch shape ---------------------------------------------------------------

def test_more_than_100_events_is_422(client, db):
    resp = _post(client, [_event(n) for n in range(1, 102)])
    assert resp.status_code == 422
    assert db.query(PlatformEvent).count() == 0


def test_body_without_events_list_is_422(client):
    resp = client.post("/integrations/events", json={"nope": []},
                       headers={"X-Platform": "ielts", "X-API-Key": IELTS_KEY})
    assert resp.status_code == 422


# --- outcomes ------------------------------------------------------------------

def test_200_with_per_event_outcomes(client, db):
    good = _event(1)
    dup = _event(1)
    bad = dict(_event(2), occurred_at="not a date")

    resp = _post(client, [good, dup, bad])

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == [good["event_id"]]
    assert body["duplicates"] == [dup["event_id"]]
    assert [r["event_id"] for r in body["rejected"]] == [bad["event_id"]]
    assert "occurred_at" in body["rejected"][0]["reason"]
    assert db.query(PlatformWeeklySet).one().title == "Week 1"


def test_empty_batch_is_200_noop(client):
    resp = _post(client, [])
    assert resp.status_code == 200
    assert resp.json() == {"accepted": [], "duplicates": [], "rejected": []}
