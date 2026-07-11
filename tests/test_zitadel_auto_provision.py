"""user.created -> Zitadel auto-provisioning (p10 trigger + drainer delivery handler).

Same two test styles as the rest of the sync suite: drainer paths on SQLite with the Zitadel
client mocked; the p10 trigger (installed TOGETHER with p9 to prove their interplay) against a
throwaway Postgres schema, skipped when no Postgres is reachable.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.courses.models import StudentSyncOutbox
from src.services import student_sync
from src.services import zitadel_provisioning as zp


class _R:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    StudentSyncOutbox.__table__.create(bind=engine)
    UserInDB.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _mk_user(db, email="new@x.io", cauid=None):
    u = UserInDB(email=email, name="New Comer", role="student", is_active=True,
                 hashed_password="$2b$12$hash", central_auth_user_id=cauid)
    db.add(u)
    db.commit()
    return u


def _seed_created(db, user_id, email="new@x.io"):
    payload = {"event_id": f"evt-c-{user_id}", "event_type": "user.created",
               "source": "lms_db_trigger",
               "user": {"lms_user_id": user_id, "email": email, "name": "New Comer", "role": "student"}}
    row = StudentSyncOutbox(event_id=payload["event_id"], event_type="user.created", payload=payload)
    db.add(row)
    db.commit()
    return row


def _seed_upserted(db, user_id, email="new@x.io", old_email="old@x.io", name="Re Named"):
    payload = {"event_id": f"evt-u-{user_id}", "event_type": "user.upserted", "source": "lms_db_trigger",
               "user": {"lms_user_id": user_id, "email": email, "old_email": old_email,
                        "name": name, "role": "student", "is_active": True, "central_auth_user_id": None}}
    row = StudentSyncOutbox(event_id=payload["event_id"], event_type="user.upserted", payload=payload)
    db.add(row)
    db.commit()
    return row


# --- drainer delivery handler ------------------------------------------------

def test_user_upserted_updates_zitadel_identity(db, monkeypatch):
    # The fix: an email change must reach Zitadel, not only SAT/IELTS.
    monkeypatch.setenv("ZITADEL_PAT", "pat")
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    u = _mk_user(db, email="new@x.io", cauid="z-linked")   # LMS already holds the NEW email + link
    _seed_upserted(db, u.id, email="new@x.io", old_email="old@x.io")
    monkeypatch.setattr(student_sync.httpx, "post", lambda url, json, headers, timeout: _R(200))  # SAT/IELTS ok
    seen = {}
    def _upd(zid, *, email=None, name=None, old_email=None, is_active=None):
        seen.update(zid=zid, email=email, old_email=old_email, name=name, is_active=is_active)
        return True
    from src.services import zitadel_provisioning as zp
    monkeypatch.setattr(zp, "update_user", _upd)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    assert db.query(StudentSyncOutbox).one().status == "done"
    # name comes from the freshest source (the DB row), email/old_email from the event.
    assert seen == {"zid": "z-linked", "email": "new@x.io", "old_email": "old@x.io",
                    "name": "New Comer", "is_active": True}


def test_user_upserted_unlinked_user_skips_zitadel(db, monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "pat")
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    u = _mk_user(db, email="new@x.io", cauid=None)         # not linked to Zitadel
    _seed_upserted(db, u.id)
    monkeypatch.setattr(student_sync.httpx, "post", lambda url, json, headers, timeout: _R(200))
    from src.services import zitadel_provisioning as zp
    monkeypatch.setattr(zp, "update_user", lambda *a, **k: pytest.fail("must not call for unlinked user"))
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1


def test_user_upserted_zitadel_failure_retries(db, monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "pat")
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    u = _mk_user(db, email="new@x.io", cauid="z-linked")
    _seed_upserted(db, u.id)
    monkeypatch.setattr(student_sync.httpx, "post", lambda url, json, headers, timeout: _R(200))
    from src.services import zitadel_provisioning as zp
    monkeypatch.setattr(zp, "update_user", lambda *a, **k: False)
    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    assert db.query(StudentSyncOutbox).one().status == "pending"


def test_unconfigured_zitadel_completes_as_noop(db, monkeypatch):
    monkeypatch.delenv("ZITADEL_PAT", raising=False)
    u = _mk_user(db)
    _seed_created(db, u.id)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "done"                       # bulk import reconciles later
    db.refresh(u)
    assert u.central_auth_user_id is None


def test_provisions_and_links_new_user(db, monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "pat")
    u = _mk_user(db)
    _seed_created(db, u.id)
    seen = {}
    def _prov(email, name, hashed):
        seen.update(email=email, hashed=hashed)
        return "z-123"
    monkeypatch.setattr(zp, "provision_user", _prov)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    db.refresh(u)
    assert u.central_auth_user_id == "z-123"
    assert seen["email"] == "new@x.io"
    assert seen["hashed"] == "$2b$12$hash"            # hash read fresh from the row, not the payload


def test_already_linked_user_is_noop(db, monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "pat")
    u = _mk_user(db, cauid="z-existing")
    _seed_created(db, u.id)
    monkeypatch.setattr(zp, "provision_user",
                        lambda *a, **k: pytest.fail("must not provision an already-linked user"))
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    db.refresh(u)
    assert u.central_auth_user_id == "z-existing"


def test_deleted_user_completes(db, monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "pat")
    _seed_created(db, user_id=424242)                 # no such users row
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1


def test_zitadel_error_retries_with_budget(db, monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "pat")
    u = _mk_user(db)
    _seed_created(db, u.id)
    def _boom(*a, **k):
        raise zp.ZitadelError("api down")
    monkeypatch.setattr(zp, "provision_user", _boom)
    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending" and row.attempts == 1
    assert "api down" in row.last_error


# --- p10 trigger (Postgres-only; installed together with p9) -----------------

_PG_URL = os.getenv("SYNC_TEST_POSTGRES_URL", "postgresql://myuser:password@localhost:5432/lms_test")
_TEST_SCHEMA = "sync_user_created_test"


@pytest.fixture()
def pg():
    try:
        engine = create_engine(_PG_URL)
        raw = engine.raw_connection()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for trigger test: {exc}")
    cur = raw.cursor()
    try:
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        cur.execute(
            "CREATE TABLE users (id serial PRIMARY KEY, email text, name text, role text, "
            "is_active boolean DEFAULT true, hashed_password text, central_auth_user_id text)"
        )
        cur.execute(
            "CREATE TABLE student_sync_outbox (id serial PRIMARY KEY, event_id text UNIQUE, "
            "event_type text, payload json, status text, attempts int, created_at timestamp)"
        )
        # BOTH users triggers, as in production — proves insert fires only p10, update only p9.
        cur.execute(student_sync.USER_SYNC_TRIGGER_UP_SQL)
        cur.execute(student_sync.USER_CREATED_TRIGGER_UP_SQL)
        raw.commit()
        yield cur
    finally:
        cur.execute("SET search_path TO public")
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
        raw.commit()
        cur.close()
        raw.close()
        engine.dispose()


def _events(pg):
    pg.execute(f"SELECT event_type, payload FROM {_TEST_SCHEMA}.student_sync_outbox ORDER BY id")
    return pg.fetchall()


def test_insert_fires_user_created_only(pg):
    pg.execute(
        f"INSERT INTO {_TEST_SCHEMA}.users (email, name, role, hashed_password) "
        f"VALUES ('n@x.io', 'New Comer', 'student', 'h')"
    )
    events = _events(pg)
    assert [e[0] for e in events] == ["user.created"]
    u = events[0][1]["user"]
    assert u["email"] == "n@x.io" and u["role"] == "student"
    assert "hashed_password" not in events[0][1]["user"]  # never in the payload


def test_insert_without_email_is_silent(pg):
    pg.execute(
        f"INSERT INTO {_TEST_SCHEMA}.users (email, name, role, hashed_password) "
        f"VALUES (NULL, 'No Mail', 'student', 'h')"
    )
    pg.execute(
        f"INSERT INTO {_TEST_SCHEMA}.users (email, name, role, hashed_password) "
        f"VALUES ('not-an-email', 'Bad Mail', 'student', 'h')"
    )
    assert _events(pg) == []


def test_insert_then_update_fires_each_trigger_once(pg):
    pg.execute(
        f"INSERT INTO {_TEST_SCHEMA}.users (email, name, role, hashed_password) "
        f"VALUES ('n@x.io', 'New Comer', 'student', 'h')"
    )
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET name = 'Renamed' WHERE email = 'n@x.io'")
    assert [e[0] for e in _events(pg)] == ["user.created", "user.upserted"]
