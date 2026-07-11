"""LMS publisher side of cross-platform sync (SSO_SYNC_DESIGN.md).

Two mechanisms, two test styles:
  * The DRAINER (this module, SQLite): outbox rows are seeded directly and the HTTP target is
    mocked, exercising the real drain/query/backoff paths without a broker or a live SAT.
  * The ENQUEUE TRIGGER (`p7`, Postgres-only): a DB trigger on `groups` writes the outbox row so
    CRM cross-DB writes are captured too. Tested against a throwaway Postgres schema; SKIPPED when
    no Postgres is reachable (so SQLite-only / CI-without-PG runs stay green).
"""

import os

import pytest
from sqlalchemy import create_engine, text
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


def _snapshot(gid=55, name="NUET-A", program="nuet", active=True):
    """The payload shape the p7 trigger produces (and the drainer publishes)."""
    return {
        "event_id": f"evt-{gid}",
        "event_type": "group.upserted",
        "source": "lms_db_trigger",
        "group": {"lms_group_id": gid, "name": name, "program_type": program, "is_active": active},
    }


def _seed(db, payload=None):
    payload = payload or _snapshot()
    row = StudentSyncOutbox(event_id=payload["event_id"], event_type="group.upserted", payload=payload)
    db.add(row)
    db.commit()
    return row


# --- flag gating -----------------------------------------------------------

def test_sync_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SYNC_ENABLED", raising=False)
    assert student_sync.sync_enabled() is False


def test_sync_enabled_truthy(monkeypatch):
    monkeypatch.setenv("SYNC_ENABLED", "true")
    assert student_sync.sync_enabled() is True


# --- drainer ---------------------------------------------------------------

class _Resp:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text


def test_drain_publishes_on_2xx(db, monkeypatch):
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    _seed(db)
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
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    _seed(db)
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
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    _seed(db)
    monkeypatch.setattr(student_sync.httpx, "post", lambda *a, **k: _Resp(503, "disabled"))

    for _ in range(12):  # far past _MAX_ATTEMPTS
        db.query(StudentSyncOutbox).update({StudentSyncOutbox.next_attempt_at: None})
        db.commit()
        student_sync.drain_outbox(db)
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.attempts == 0


def test_drain_retries_on_5xx_then_backs_off(db, monkeypatch):
    # A genuine server error (500) DOES spend an attempt and backs off.
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    _seed(db)
    monkeypatch.setattr(student_sync.httpx, "post", lambda *a, **k: _Resp(500, "boom"))

    result = student_sync.drain_outbox(db)
    assert result == {"published": 0, "failed": 0, "retried": 1}
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at is not None
    assert "500" in row.last_error


def test_drain_transport_error_is_retried(db, monkeypatch):
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    _seed(db)
    import httpx as _httpx

    def boom(*a, **k):
        raise _httpx.ConnectError("no route")
    monkeypatch.setattr(student_sync.httpx, "post", boom)

    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    assert "transport error" in db.query(StudentSyncOutbox).one().last_error


# --- multi-target fan-out --------------------------------------------------

def _seed_member(db, eid="m1"):
    payload = {
        "event_id": eid, "event_type": "member.upserted", "source": "lms_db_trigger",
        "lms_group_id": 9, "program_type": "nuet",
        "student": {"email": "s@x.io", "central_auth_user_id": "c", "name": "S"},
    }
    row = StudentSyncOutbox(event_id=eid, event_type="member.upserted", payload=payload)
    db.add(row); db.commit()
    return row


def _capture_posts(monkeypatch, code=200):
    posts = []
    monkeypatch.setattr(student_sync.httpx, "post",
                        lambda url, json, headers, timeout: posts.append((url, headers.get("X-API-Key"))) or _Resp(code))
    return posts


def test_member_fans_out_only_to_sat_when_ielts_dormant(db, monkeypatch):
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    monkeypatch.delenv("IELTS_SYNC_URL", raising=False)     # IELTS not configured
    _seed_member(db)
    posts = _capture_posts(monkeypatch)
    student_sync.drain_outbox(db)
    assert len(posts) == 1                                   # SAT only
    assert posts[0][0].endswith("/api/lms/students/membership")
    assert db.query(StudentSyncOutbox).one().status == "done"


def test_member_fans_out_to_both_when_ielts_configured(db, monkeypatch):
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")
    monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/api")
    monkeypatch.setenv("IELTS_API_KEY", "kielts")
    _seed_member(db)
    posts = _capture_posts(monkeypatch)
    student_sync.drain_outbox(db)
    assert len(posts) == 2                                   # SAT + IELTS
    bases = sorted(u for u, _ in posts)
    assert any("api.mastereducation.kz" in u for u, _ in posts)
    assert any(u.startswith("https://ielts.example/api/students/membership") for u, _ in posts)
    assert db.query(StudentSyncOutbox).one().status == "done"


def test_group_event_routes_to_both_platforms(db, monkeypatch):
    # group.upserted now also carries the teacher/curator, so IELTS consumes it too (its own path).
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")
    monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/api")
    monkeypatch.setenv("IELTS_API_KEY", "kielts")
    _seed(db)                                                # a group.upserted
    posts = _capture_posts(monkeypatch)
    student_sync.drain_outbox(db)
    assert len(posts) == 2
    assert any(u.endswith("/api/lms/groups") for u, _ in posts)          # SAT
    assert any(u.endswith("/api/students/groups") for u, _ in posts)     # IELTS


def test_fanout_not_ready_if_any_target_503(db, monkeypatch):
    # SAT ok but IELTS 503 => reschedule without spending the budget (re-post to SAT is idempotent).
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")
    monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/api")
    monkeypatch.setenv("IELTS_API_KEY", "kielts")
    _seed_member(db)
    def post(url, json, headers, timeout):
        return _Resp(200) if "mastereducation" in url else _Resp(503, "off")
    monkeypatch.setattr(student_sync.httpx, "post", post)
    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending"
    assert row.attempts == 0                                 # not_ready doesn't spend budget


# --- enqueue trigger (Postgres-only; skips when no PG reachable) ------------

_PG_URL = os.getenv("SYNC_TEST_POSTGRES_URL", "postgresql://myuser:password@localhost:5432/lms_test")
_TEST_SCHEMA = "sync_trigger_test"


@pytest.fixture()
def pg():
    """A throwaway Postgres schema with a minimal `groups` + `student_sync_outbox` and the REAL
    p7 trigger installed. Yields a raw psycopg2 cursor (no SQLAlchemy param-substitution, so the
    multi-statement PL/pgSQL DDL runs verbatim). Skips the test if Postgres is not reachable."""
    try:
        engine = create_engine(_PG_URL)
        raw = engine.raw_connection()           # establishes the connection (auth fails here if bad)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for trigger test: {exc}")
    cur = raw.cursor()
    try:
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        # Minimal shapes: only the columns the trigger reads/writes.
        cur.execute("CREATE TABLE users (id serial PRIMARY KEY, email text, name text)")
        cur.execute(
            "CREATE TABLE groups (id serial PRIMARY KEY, name text, program_type text, "
            "is_active boolean DEFAULT true, description text, teacher_id int, curator_id int)"
        )
        cur.execute(
            "CREATE TABLE student_sync_outbox (id serial PRIMARY KEY, event_id text UNIQUE, "
            "event_type text, payload json, status text, attempts int, created_at timestamp)"
        )
        # Install the REAL production trigger SQL in one shot (same string the migration executes).
        cur.execute(student_sync.GROUP_SYNC_TRIGGER_UP_SQL)
        raw.commit()
        yield cur
    finally:
        cur.execute("SET search_path TO public")
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
        raw.commit()
        cur.close()
        raw.close()
        engine.dispose()


def _outbox_payloads(pg):
    pg.execute(f"SELECT payload FROM {_TEST_SCHEMA}.student_sync_outbox ORDER BY id")
    return [r[0] for r in pg.fetchall()]         # psycopg2 decodes json -> dict


def test_trigger_enqueues_on_group_insert(pg):
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type, is_active) VALUES ('SAT-A', 'sat', true)")
    payloads = _outbox_payloads(pg)
    assert len(payloads) == 1
    p = payloads[0]
    assert p["event_type"] == "group.upserted"
    assert p["group"]["program_type"] == "sat"
    assert p["group"]["name"] == "SAT-A"
    assert p["group"]["is_active"] is True
    assert p["group"]["lms_group_id"] >= 1
    assert p["group"]["old_name"] == "SAT-A"       # on INSERT, old_name == name (no rename)
    assert p["event_id"]                          # a generated event_id is present
    # event_id is mirrored into both the column and the payload (the drainer's dedup key)
    pg.execute(f"SELECT event_id FROM {_TEST_SCHEMA}.student_sync_outbox ORDER BY id LIMIT 1")
    assert pg.fetchone()[0] == p["event_id"]


def test_trigger_enqueues_on_synced_field_update(pg):
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type, is_active) VALUES ('N', 'nuet', true)")
    pg.execute(f"UPDATE {_TEST_SCHEMA}.groups SET name = 'N-renamed'")
    payloads = _outbox_payloads(pg)
    assert len(payloads) == 2                     # insert + rename both enqueued
    assert payloads[1]["group"]["name"] == "N-renamed"
    # A rename carries the PREVIOUS name so the name-keyed IELTS consumer can find + rename it.
    assert payloads[1]["group"]["old_name"] == "N"


def test_trigger_skips_update_of_unsynced_field(pg):
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type, is_active) VALUES ('N', 'nuet', true)")
    pg.execute(f"UPDATE {_TEST_SCHEMA}.groups SET description = 'just a note'")
    payloads = _outbox_payloads(pg)
    assert len(payloads) == 1                     # description change did NOT enqueue


def test_trigger_coalesces_null_is_active_to_true(pg):
    # is_active is nullable in the LMS model; the payload must never carry JSON null (the SAT
    # consumer binds a non-nullable bool) — a NULL is emitted as true (active).
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type, is_active) VALUES ('X', 'sat', NULL)")
    payloads = _outbox_payloads(pg)
    assert len(payloads) == 1
    assert payloads[0]["group"]["is_active"] is True


def test_trigger_embeds_teacher_and_curator_identity(pg):
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.users (email, name) VALUES ('teach@x.io','Ms Teach')")
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.users (email, name) VALUES ('cur@x.io','Mr Cur')")
    pg.execute(
        f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type, teacher_id, curator_id) "
        f"VALUES ('SAT-A', 'sat', 1, 2)"
    )
    g = _outbox_payloads(pg)[0]["group"]
    assert g["teacher_email"] == "teach@x.io" and g["teacher_name"] == "Ms Teach"
    assert g["curator_email"] == "cur@x.io" and g["curator_name"] == "Mr Cur"


def test_trigger_fires_on_teacher_reassignment(pg):
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.users (email, name) VALUES ('t1@x.io','T1')")
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type) VALUES ('G', 'sat')")  # insert => 1
    pg.execute(f"UPDATE {_TEST_SCHEMA}.groups SET teacher_id = 1")                              # reassign => 2
    payloads = _outbox_payloads(pg)
    assert len(payloads) == 2
    assert payloads[1]["group"]["teacher_email"] == "t1@x.io"


def test_trigger_unassigned_teacher_is_null(pg):
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type) VALUES ('G', 'sat')")
    g = _outbox_payloads(pg)[0]["group"]
    assert g["teacher_email"] is None and g["curator_email"] is None  # consumer clears the FK


def test_trigger_captures_generic_writer(pg):
    # The whole point: a writer that is NOT the LMS app (here: raw SQL, standing in for the CRM's
    # cross-DB write) still produces an outbox row — the app hook could never have seen this.
    pg.execute(f"INSERT INTO {_TEST_SCHEMA}.groups (name, program_type) VALUES ('crm-made', 'sat')")
    payloads = _outbox_payloads(pg)
    assert len(payloads) == 1
    assert payloads[0]["group"]["name"] == "crm-made"


# --- membership trigger (Postgres-only; skips when no PG reachable) ---------

@pytest.fixture()
def pg_member():
    """Throwaway schema with minimal users/groups/group_students + the REAL p8 member trigger."""
    try:
        engine = create_engine(_PG_URL)
        raw = engine.raw_connection()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for member-trigger test: {exc}")
    cur = raw.cursor()
    try:
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        cur.execute("CREATE TABLE users (id serial PRIMARY KEY, email text, central_auth_user_id text, name text)")
        cur.execute("CREATE TABLE groups (id serial PRIMARY KEY, name text, program_type text, "
                    "teacher_id int, curator_id int)")
        cur.execute(
            "CREATE TABLE group_students (id serial PRIMARY KEY, group_id int, student_id int)"
        )
        cur.execute(
            "CREATE TABLE student_sync_outbox (id serial PRIMARY KEY, event_id text UNIQUE, "
            "event_type text, payload json, status text, attempts int, created_at timestamp)"
        )
        cur.execute(student_sync.MEMBER_SYNC_TRIGGER_UP_SQL)
        # seed one student, a teacher + curator, and one group that references them
        cur.execute("INSERT INTO users (id, email, central_auth_user_id, name) "
                    "VALUES (7, 'stu@x.io', 'central-abc', 'Stu Dent')")
        cur.execute("INSERT INTO users (id, email, name) VALUES (3, 'teach@x.io', 'Ms Teach')")
        cur.execute("INSERT INTO users (id, email, name) VALUES (4, 'cur@x.io', 'Mr Cur')")
        cur.execute("INSERT INTO groups (id, name, program_type, teacher_id, curator_id) "
                    "VALUES (9, 'NUET-A', 'nuet', 3, 4)")
        raw.commit()
        yield cur
    finally:
        cur.execute("SET search_path TO public")
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
        raw.commit()
        cur.close()
        raw.close()
        engine.dispose()


def test_member_trigger_enqueues_upserted_on_insert(pg_member):
    pg_member.execute(f"INSERT INTO {_TEST_SCHEMA}.group_students (group_id, student_id) VALUES (9, 7)")
    payloads = _outbox_payloads(pg_member)
    assert len(payloads) == 1
    p = payloads[0]
    assert p["event_type"] == "member.upserted"
    assert p["lms_group_id"] == 9
    assert p["program_type"] == "nuet"
    assert p["group_name"] == "NUET-A"
    assert p["student"]["email"] == "stu@x.io"
    assert p["student"]["central_auth_user_id"] == "central-abc"
    assert p["student"]["name"] == "Stu Dent"
    assert p["student"]["lms_student_id"] == 7
    # the group's teacher/curator ride along so a lazy group create can link them on first join
    assert p["teacher_email"] == "teach@x.io" and p["teacher_name"] == "Ms Teach"
    assert p["curator_email"] == "cur@x.io" and p["curator_name"] == "Mr Cur"


def test_member_trigger_enqueues_removed_on_delete(pg_member):
    pg_member.execute(f"INSERT INTO {_TEST_SCHEMA}.group_students (group_id, student_id) VALUES (9, 7)")
    pg_member.execute(f"DELETE FROM {_TEST_SCHEMA}.group_students WHERE group_id = 9 AND student_id = 7")
    payloads = _outbox_payloads(pg_member)
    assert len(payloads) == 2
    assert payloads[0]["event_type"] == "member.upserted"
    assert payloads[1]["event_type"] == "member.removed"
    assert payloads[1]["student"]["email"] == "stu@x.io"   # identity resolved even on delete
    assert payloads[1]["lms_group_id"] == 9


def test_member_trigger_captures_generic_writer(pg_member):
    # A non-app writer (raw SQL == CRM cross-DB enrollment) is captured too.
    pg_member.execute(f"INSERT INTO {_TEST_SCHEMA}.group_students (group_id, student_id) VALUES (9, 7)")
    payloads = _outbox_payloads(pg_member)
    assert len(payloads) == 1
    assert payloads[0]["source"] == "lms_db_trigger"


def test_member_drain_routes_to_membership_endpoint(db, monkeypatch):
    # The drainer must POST member events to /students/membership (not /groups).
    monkeypatch.setenv("MASTEREDU_API_KEY", "k")
    payload = {
        "event_id": "m1", "event_type": "member.upserted", "source": "lms_db_trigger",
        "lms_group_id": 9, "program_type": "nuet",
        "student": {"email": "stu@x.io", "central_auth_user_id": "central-abc", "name": "S"},
    }
    row = StudentSyncOutbox(event_id="m1", event_type="member.upserted", payload=payload)
    db.add(row); db.commit()
    sent = {}
    monkeypatch.setattr(student_sync.httpx, "post",
                        lambda url, json, headers, timeout: sent.update(url=url) or _Resp(200))
    student_sync.drain_outbox(db)
    assert sent["url"].endswith("/api/lms/students/membership")
    assert db.query(StudentSyncOutbox).one().status == "done"


# --- backfill (Postgres-only; skips when no PG reachable) -------------------

@pytest.fixture()
def pg_session():
    """Throwaway schema with groups/users/group_students/outbox + a Session whose search_path
    points at it, so the backfill's unqualified SQL resolves there. Skips if no Postgres."""
    from src.services import sync_backfill  # noqa: F401 - ensure importable
    try:
        admin = create_engine(_PG_URL)
        raw = admin.raw_connection()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres not reachable for backfill test: {exc}")
    cur = raw.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {_TEST_SCHEMA}")
    cur.execute(f"SET search_path TO {_TEST_SCHEMA}")
    cur.execute("CREATE TABLE groups (id serial PRIMARY KEY, name text, program_type text, "
                "is_active boolean, teacher_id int, curator_id int)")
    cur.execute("CREATE TABLE users (id serial PRIMARY KEY, email text, central_auth_user_id text, name text)")
    cur.execute("CREATE TABLE group_students (id serial PRIMARY KEY, group_id int, student_id int)")
    cur.execute("CREATE TABLE student_sync_outbox (id serial PRIMARY KEY, event_id text UNIQUE, "
                "event_type text, payload json, status text, attempts int, created_at timestamp)")
    # 4 groups (sat, nuet, ielts, general_english), 3 students, memberships incl. a general_english one
    cur.execute("INSERT INTO groups (id, name, program_type, is_active) VALUES "
                "(1,'SAT-A','sat',true),(2,'NUET-B','nuet',true),(3,'IELTS-C','ielts',true),(4,'GE-D','general_english',true)")
    cur.execute("INSERT INTO users (id, email, central_auth_user_id, name) VALUES "
                "(1,'a@x.io','c1','A'),(2,'b@x.io',NULL,'B'),(3,'c@x.io','c3','C')")
    cur.execute("INSERT INTO group_students (group_id, student_id) VALUES (1,1),(2,2),(3,3),(4,1)")
    raw.commit(); cur.close(); raw.close()

    engine = create_engine(_PG_URL, connect_args={"options": f"-csearch_path={_TEST_SCHEMA},public"})
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close(); engine.dispose()
        r2 = admin.raw_connection(); c2 = r2.cursor()
        c2.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE"); r2.commit(); c2.close(); r2.close()
        admin.dispose()


def _obx(pg_session):
    return pg_session.execute(text(f"SELECT event_type, payload FROM {_TEST_SCHEMA}.student_sync_outbox ORDER BY id")).fetchall()


def test_backfill_groups_sat_nuet_ielts(pg_session):
    from src.services import sync_backfill
    res = sync_backfill.backfill(pg_session, do_groups=True)
    assert res["groups"] == 3                       # sat + nuet + ielts, NOT general_english
    rows = _obx(pg_session)
    assert {r[0] for r in rows} == {"group.upserted"}
    progs = {r[1]["group"]["program_type"] for r in rows}
    assert progs == {"sat", "nuet", "ielts"}
    # teacher/curator keys are always present (null here — no assignment in this fixture)
    assert all("teacher_email" in r[1]["group"] for r in rows)
    assert all(r[1]["source"] == "lms_backfill" for r in rows)


def test_backfill_members_sat_nuet_ielts(pg_session):
    from src.services import sync_backfill
    res = sync_backfill.backfill(pg_session, do_members=True)
    assert res["members"] == 3                       # sat + nuet + ielts members, NOT general_english
    rows = _obx(pg_session)
    assert {r[0] for r in rows} == {"member.upserted"}
    emails = sorted(r[1]["student"]["email"] for r in rows)
    assert emails == ["a@x.io", "b@x.io", "c@x.io"]
    one = rows[0][1]
    assert one["lms_group_id"] and one["group_name"] and one["program_type"] in ("sat", "nuet", "ielts")


def test_backfill_dry_run_enqueues_nothing(pg_session):
    from src.services import sync_backfill
    res = sync_backfill.backfill(pg_session, do_groups=True, do_members=True, dry_run=True)
    assert res == {"groups": 3, "members": 3, "dry_run": True}
    assert _obx(pg_session) == []                     # nothing written
