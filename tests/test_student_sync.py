"""LMS publisher side of cross-platform sync (SSO_SYNC_DESIGN.md): outbox enqueue + drainer.

SQLite-backed (only the outbox table) with the HTTP target mocked — exercises the real
enqueue/drain query paths without a broker or a live SAT."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.courses.models import StudentSyncOutbox
from src.services import student_sync


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    StudentSyncOutbox.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _group(gid=55, name="NUET-A", program="nuet", active=True):
    return SimpleNamespace(id=gid, name=name, program_type=program, is_active=active)


# --- flag gating -----------------------------------------------------------

def test_sync_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SYNC_ENABLED", raising=False)
    assert student_sync.sync_enabled() is False


def test_enqueue_noop_when_disabled(db, monkeypatch):
    monkeypatch.delenv("SYNC_ENABLED", raising=False)
    assert student_sync.enqueue_group_upserted(db, _group()) is None
    db.commit()
    assert db.query(StudentSyncOutbox).count() == 0


# --- enqueue ---------------------------------------------------------------

def test_enqueue_writes_snapshot_row(db, monkeypatch):
    monkeypatch.setenv("SYNC_ENABLED", "true")
    row = student_sync.enqueue_group_upserted(db, _group())
    db.commit()
    assert row is not None
    saved = db.query(StudentSyncOutbox).one()
    assert saved.event_type == "group.upserted"
    assert saved.status == "pending"
    assert saved.payload["group"] == {
        "lms_group_id": 55, "name": "NUET-A", "program_type": "nuet", "is_active": True,
    }
    assert saved.event_id == saved.payload["event_id"]


# --- drainer ---------------------------------------------------------------

class _Resp:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text


def test_drain_publishes_on_2xx(db, monkeypatch):
    monkeypatch.setenv("SYNC_ENABLED", "true")
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    student_sync.enqueue_group_upserted(db, _group())
    db.commit()
    sent = {}
    def fake_post(url, json, headers, timeout):
        sent["url"] = url; sent["json"] = json; sent["key"] = headers.get("X-API-Key")
        return _Resp(200)
    monkeypatch.setattr(student_sync.httpx, "post", fake_post)

    result = student_sync.drain_outbox(db)
    assert result == {"published": 1, "failed": 0, "retried": 0}
    assert db.query(StudentSyncOutbox).one().status == "done"
    assert sent["url"].endswith("/api/lms/groups")
    assert sent["key"] == "k"
    assert sent["json"]["group"]["lms_group_id"] == 55


def test_drain_503_reschedules_without_spending_attempts(db, monkeypatch):
    # 503 = consumer deployed but its sync flag is off. Must reschedule WITHOUT burning the
    # retry budget so a rollout window can't dead-letter valid events.
    monkeypatch.setenv("SYNC_ENABLED", "true")
    student_sync.enqueue_group_upserted(db, _group())
    db.commit()
    monkeypatch.setattr(student_sync.httpx, "post", lambda *a, **k: _Resp(503, "disabled"))

    result = student_sync.drain_outbox(db)
    assert result == {"published": 0, "failed": 0, "retried": 1}
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"        # still pending, will retry
    assert row.attempts == 0              # 503 does NOT spend an attempt
    assert row.next_attempt_at is not None
    assert "503" in row.last_error


def test_persistent_503_never_dead_letters(db, monkeypatch):
    # Consumer left disabled indefinitely: the event must stay pending forever (never "failed"),
    # so it self-heals the moment the operator enables the consumer.
    monkeypatch.setenv("SYNC_ENABLED", "true")
    student_sync.enqueue_group_upserted(db, _group())
    db.commit()
    monkeypatch.setattr(student_sync.httpx, "post", lambda *a, **k: _Resp(503, "disabled"))

    for _ in range(_MANY := 12):  # far past _MAX_ATTEMPTS
        # clear the backoff so each pass re-attempts this row
        db.query(StudentSyncOutbox).update({StudentSyncOutbox.next_attempt_at: None})
        db.commit()
        student_sync.drain_outbox(db)
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.attempts == 0


def test_drain_retries_on_5xx_then_backs_off(db, monkeypatch):
    # A genuine server error (500) DOES spend an attempt and backs off.
    monkeypatch.setenv("SYNC_ENABLED", "true")
    student_sync.enqueue_group_upserted(db, _group())
    db.commit()
    monkeypatch.setattr(student_sync.httpx, "post", lambda *a, **k: _Resp(500, "boom"))

    result = student_sync.drain_outbox(db)
    assert result == {"published": 0, "failed": 0, "retried": 1}
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"        # still pending, will retry
    assert row.attempts == 1
    assert row.next_attempt_at is not None
    assert "500" in row.last_error


def test_drain_transport_error_is_retried(db, monkeypatch):
    monkeypatch.setenv("SYNC_ENABLED", "true")
    student_sync.enqueue_group_upserted(db, _group())
    db.commit()
    import httpx as _httpx

    def boom(*a, **k):
        raise _httpx.ConnectError("no route")
    monkeypatch.setattr(student_sync.httpx, "post", boom)

    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    assert "transport error" in db.query(StudentSyncOutbox).one().last_error
