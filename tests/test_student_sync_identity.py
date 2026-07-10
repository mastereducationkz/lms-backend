"""user.upserted identity sync — LMS publisher side (p9 trigger + drainer routing + replay CLI).

Mirrors tests/test_student_sync.py's two styles:
  * DRAINER routing + the replay tool on SQLite with a mocked HTTP target.
  * The p9 ENQUEUE TRIGGER against a throwaway Postgres schema (skipped without Postgres),
    running the REAL production DDL string the migration executes.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.courses.models import StudentSyncOutbox
from src.services import student_sync, sync_replay


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    StudentSyncOutbox.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user_event(uid=7, email="new@x.io", old_email="old@x.io", name="Re Named", role="student"):
    """The payload shape the p9 trigger produces."""
    return {
        "event_id": f"evt-user-{uid}",
        "event_type": "user.upserted",
        "source": "lms_db_trigger",
        "user": {
            "lms_user_id": uid,
            "email": email,
            "old_email": old_email,
            "name": name,
            "role": role,
            "is_active": True,
            "central_auth_user_id": None,
        },
    }


def _seed_user_event(db, payload=None, status="pending"):
    payload = payload or _user_event()
    row = StudentSyncOutbox(
        event_id=payload["event_id"], event_type="user.upserted", payload=payload, status=status
    )
    db.add(row)
    db.commit()
    return row


class _Resp:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text


def _capture_posts(monkeypatch, code=200):
    posts = []
    monkeypatch.setattr(
        student_sync.httpx, "post",
        lambda url, json, headers, timeout: posts.append((url, json)) or _Resp(code),
    )
    return posts


# --- drainer routing ---------------------------------------------------------

def test_user_upserted_fans_out_to_both_platforms(db, monkeypatch):
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")
    monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/api")
    monkeypatch.setenv("IELTS_API_KEY", "kielts")
    _seed_user_event(db)
    posts = _capture_posts(monkeypatch)
    student_sync.drain_outbox(db)
    assert len(posts) == 2
    assert all(u.endswith("/students/identity") for u, _ in posts)
    assert db.query(StudentSyncOutbox).one().status == "done"


def test_user_upserted_sat_only_when_ielts_dormant(db, monkeypatch):
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")
    monkeypatch.delenv("IELTS_SYNC_URL", raising=False)
    _seed_user_event(db)
    posts = _capture_posts(monkeypatch)
    student_sync.drain_outbox(db)
    assert len(posts) == 1
    assert "api.mastereducation.kz" in posts[0][0]
    assert posts[0][1]["user"]["old_email"] == "old@x.io"  # match key rides along


# --- replay CLI --------------------------------------------------------------

def test_replay_requeues_failed_rows(db):
    row = _seed_user_event(db, status="failed")
    row.attempts = 8
    row.last_error = "sat: HTTP 404: student not found"
    db.commit()
    result = sync_replay.replay(db)
    assert result["replayed"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.last_error is not None  # preserved until next attempt for the operator


def test_replay_filters_by_event_type_and_error(db):
    a = _seed_user_event(db, _user_event(uid=1), status="failed")
    b = _seed_user_event(db, _user_event(uid=2), status="failed")
    a.last_error = "ielts: HTTP 404: user not found"
    b.last_error = "sat: transport error"
    db.commit()
    result = sync_replay.replay(db, like_error="not found")
    assert result["replayed"] == 1
    assert db.query(StudentSyncOutbox).filter_by(status="pending").one().payload["user"]["lms_user_id"] == 1


def test_replay_dry_run_mutates_nothing(db):
    _seed_user_event(db, status="failed")
    result = sync_replay.replay(db, dry_run=True)
    assert result["replayed"] == 1 and result["dry_run"] is True
    assert db.query(StudentSyncOutbox).one().status == "failed"


def test_replay_ignores_pending_and_done(db):
    _seed_user_event(db, _user_event(uid=1), status="pending")
    _seed_user_event(db, _user_event(uid=2), status="done")
    assert sync_replay.replay(db)["replayed"] == 0


def test_summarize_counts_by_type(db):
    _seed_user_event(db, status="failed")
    s = sync_replay.summarize(db)
    assert s == {"failed_total": 1, "by_event_type": {"user.upserted": 1}}


# --- enqueue trigger (Postgres-only; skips when no PG reachable) ------------

_PG_URL = os.getenv("SYNC_TEST_POSTGRES_URL", "postgresql://myuser:password@localhost:5432/lms_test")
_TEST_SCHEMA = "sync_user_trigger_test"


@pytest.fixture()
def pg():
    """Throwaway Postgres schema with a minimal `users` + outbox and the REAL p9 trigger."""
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
        # Minimal shape: only the columns the trigger reads (plus one it must ignore).
        cur.execute(
            "CREATE TABLE users (id serial PRIMARY KEY, email text, name text, role text, "
            "is_active boolean DEFAULT true, hashed_password text, central_auth_user_id text, "
            "student_id text)"
        )
        cur.execute(
            "CREATE TABLE student_sync_outbox (id serial PRIMARY KEY, event_id text UNIQUE, "
            "event_type text, payload json, status text, attempts int, created_at timestamp)"
        )
        cur.execute(student_sync.USER_SYNC_TRIGGER_UP_SQL)
        raw.commit()
        yield cur
    finally:
        cur.execute("SET search_path TO public")
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
        raw.commit()
        cur.close()
        raw.close()
        engine.dispose()


def _payloads(pg):
    pg.execute(f"SELECT payload FROM {_TEST_SCHEMA}.student_sync_outbox ORDER BY id")
    return [r[0] for r in pg.fetchall()]


def _mk_user(pg, email="s@x.io", name="Stu Dent", role="student"):
    pg.execute(
        f"INSERT INTO {_TEST_SCHEMA}.users (email, name, role, hashed_password) "
        f"VALUES ('{email}', '{name}', '{role}', 'h1') RETURNING id"
    )
    return pg.fetchone()[0]


def test_insert_does_not_enqueue(pg):
    _mk_user(pg)
    assert _payloads(pg) == []  # creation flows through provisioning, not identity sync


def test_rename_enqueues_with_old_email_equal(pg):
    uid = _mk_user(pg)
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET name = 'New Name' WHERE id = {uid}")
    payloads = _payloads(pg)
    assert len(payloads) == 1
    u = payloads[0]["user"]
    assert payloads[0]["event_type"] == "user.upserted"
    assert u["name"] == "New Name"
    assert u["email"] == "s@x.io" and u["old_email"] == "s@x.io"
    assert u["role"] == "student" and u["is_active"] is True


def test_email_change_carries_old_email(pg):
    uid = _mk_user(pg, email="before@x.io")
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET email = 'after@x.io' WHERE id = {uid}")
    u = _payloads(pg)[0]["user"]
    assert u["email"] == "after@x.io"
    assert u["old_email"] == "before@x.io"  # the consumer's re-key handle


def test_password_only_change_is_silent(pg):
    uid = _mk_user(pg)
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET hashed_password = 'h2' WHERE id = {uid}")
    assert _payloads(pg) == []  # passwords are never synced platform-to-platform


def test_unsynced_field_change_is_silent(pg):
    uid = _mk_user(pg)
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET student_id = 'S-9' WHERE id = {uid}")
    assert _payloads(pg) == []


def test_role_and_deactivation_enqueue(pg):
    uid = _mk_user(pg, role="student")
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET role = 'curator' WHERE id = {uid}")
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET is_active = false WHERE id = {uid}")
    payloads = _payloads(pg)
    assert len(payloads) == 2
    assert payloads[0]["user"]["role"] == "curator"
    assert payloads[1]["user"]["is_active"] is False


def test_staff_users_are_covered(pg):
    # Teachers/curators are ordinary users rows — the trigger is the staff-sync foundation.
    uid = _mk_user(pg, email="t@x.io", name="Teach", role="teacher")
    pg.execute(f"UPDATE {_TEST_SCHEMA}.users SET name = 'Teach Renamed' WHERE id = {uid}")
    u = _payloads(pg)[0]["user"]
    assert u["role"] == "teacher" and u["name"] == "Teach Renamed"
