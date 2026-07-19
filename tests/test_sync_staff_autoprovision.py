"""Automatic teacher/curator staff provisioning to SAT + IELTS (the fix for "groups sync but the
teacher/curator slot lands empty on the platforms").

Two drainer paths are covered, both on in-memory SQLite with httpx mocked:

  * user.created  -> besides Zitadel, POST staff.upserted to every configured platform for a
                     teacher/curator (no-op for students), so their account exists day-one.
  * group.upserted-> before delivering the group, ensure its teacher/curator (looked up from the
                     payload emails) have a staff account on the group's OWN program platform, so
                     the consumer can resolve the FK (the platforms never JIT staff).

Both the group fan-out (student_sync.httpx) and the staff POST (sync_provision_staff.httpx) are
patched to one recorder so a single call list captures everything.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.courses.models import StudentSyncOutbox
from src.services import student_sync
from src.services import sync_provision_staff as sps


class _Resp:
    def __init__(self, code, json_body=None, text=""):
        self.status_code = code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


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


_uid = iter(range(1, 10_000))


def _user(db, email, *, name="Ms T", role="teacher"):
    u = UserInDB(id=next(_uid), email=email, name=name, role=role,
                 hashed_password="$2b$12$hash", is_active=True)
    db.add(u)
    db.commit()
    return u


def _seed_created(db, user_id):
    payload = {"event_id": f"evt-c-{user_id}", "event_type": "user.created",
               "source": "lms_db_trigger",
               "user": {"lms_user_id": user_id, "email": "ignored", "name": "n", "role": "ignored"}}
    row = StudentSyncOutbox(event_id=payload["event_id"], event_type="user.created", payload=payload)
    db.add(row)
    db.commit()
    return row


def _seed_group(db, *, teacher_email=None, curator_email=None, program="ielts", gid=1):
    group = {"lms_group_id": gid, "name": "G", "old_name": "G", "program_type": program,
             "is_active": True, "teacher_email": teacher_email, "teacher_name": "T",
             "curator_email": curator_email, "curator_name": "C"}
    payload = {"event_id": f"evt-g-{gid}", "event_type": "group.upserted",
               "source": "lms_db_trigger", "group": group}
    row = StudentSyncOutbox(event_id=payload["event_id"], event_type="group.upserted", payload=payload)
    db.add(row)
    db.commit()
    return row


def _env(monkeypatch, *, ielts=True, sat=True, zitadel=False):
    # Zitadel off by default so these tests isolate the staff-provisioning behaviour.
    monkeypatch.delenv("ZITADEL_PAT", raising=False)
    if zitadel:
        monkeypatch.setenv("ZITADEL_PAT", "pat")
    if ielts:
        monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/lms")
        monkeypatch.setenv("IELTS_API_KEY", "kielts")
    else:
        monkeypatch.delenv("IELTS_SYNC_URL", raising=False)
        monkeypatch.delenv("IELTS_API_KEY", raising=False)
    if sat:
        monkeypatch.setenv("SAT_SYNC_URL", "https://sat.example/api/lms")
        monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")
    else:
        monkeypatch.delenv("SAT_SYNC_URL", raising=False)
        monkeypatch.delenv("MASTEREDU_API_KEY", raising=False)


def _capture(monkeypatch, response_for=None):
    """Record every POST (group fan-out AND staff) into one list. response_for(url, body)->_Resp;
    default: 201 created for /staff, 200 for group delivery."""
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "key": headers.get("X-API-Key")})
        if response_for is not None:
            r = response_for(url, json)
            if r is not None:
                return r
        if url.endswith("/staff"):
            return _Resp(201, {"created": True})
        return _Resp(200)

    monkeypatch.setattr(student_sync.httpx, "post", fake_post)
    monkeypatch.setattr(sps.httpx, "post", fake_post)
    return calls


def _staff_calls(calls):
    return [c for c in calls if c["url"].endswith("/staff")]


# --- user.created: staff auto-provisioning -----------------------------------


def test_created_teacher_provisions_staff_on_both_platforms(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "t@x.io", role="teacher")
    _seed_created(db, t.id)
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    staff = _staff_calls(calls)
    assert sorted(c["url"] for c in staff) == [
        "https://ielts.example/lms/staff", "https://sat.example/api/lms/staff"]
    assert all(c["json"]["staff"]["role"] == "teacher" for c in staff)
    assert all(c["json"]["staff"]["email"] == "t@x.io" for c in staff)
    assert all(c["json"]["event_type"] == "staff.upserted" for c in staff)


def test_created_student_provisions_no_staff(db, monkeypatch):
    _env(monkeypatch)
    s = _user(db, "s@x.io", role="student")
    _seed_created(db, s.id)
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    assert _staff_calls(calls) == []


def test_created_curator_provisions_as_curator(db, monkeypatch):
    _env(monkeypatch)
    c = _user(db, "c@x.io", role="curator")
    _seed_created(db, c.id)
    calls = _capture(monkeypatch)
    student_sync.drain_outbox(db)
    staff = _staff_calls(calls)
    assert len(staff) == 2
    assert all(x["json"]["staff"]["role"] == "curator" for x in staff)


def test_created_head_teacher_maps_to_teacher(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "ht@x.io", role="head_teacher")
    _seed_created(db, t.id)
    calls = _capture(monkeypatch)
    student_sync.drain_outbox(db)
    assert all(x["json"]["staff"]["role"] == "teacher" for x in _staff_calls(calls))


def test_created_test_email_is_skipped(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "someone@lms.com", role="teacher")
    _seed_created(db, t.id)
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    assert _staff_calls(calls) == []


def test_created_only_configured_platform_is_hit(db, monkeypatch):
    _env(monkeypatch, sat=False)  # IELTS only
    t = _user(db, "t@x.io", role="teacher")
    _seed_created(db, t.id)
    calls = _capture(monkeypatch)
    student_sync.drain_outbox(db)
    assert [c["url"] for c in _staff_calls(calls)] == ["https://ielts.example/lms/staff"]


def test_created_staff_transient_error_retries(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "t@x.io", role="teacher")
    _seed_created(db, t.id)
    _capture(monkeypatch, response_for=lambda u, b: _Resp(500, text="boom") if u.endswith("/staff") else None)
    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending" and row.attempts == 1


def test_created_staff_503_is_not_ready_no_budget(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "t@x.io", role="teacher")
    _seed_created(db, t.id)
    _capture(monkeypatch, response_for=lambda u, b: _Resp(503, text="disabled") if u.endswith("/staff") else None)
    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    row = db.query(StudentSyncOutbox).one()
    assert row.status == "pending" and row.attempts == 0  # not_ready spends no budget


def test_created_trial_user_provisions_nothing(db, monkeypatch):
    # Trial prospects are LMS-only: no Zitadel, no platform staff (even if role is teacher).
    _env(monkeypatch, zitadel=True)
    from src.services import zitadel_provisioning as zp
    monkeypatch.setattr(zp, "provision_user", lambda *a, **k: pytest.fail("trial must not provision"))
    t = _user(db, "trial@x.io", role="teacher")
    t.is_trial = True
    db.commit()
    _seed_created(db, t.id)
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    assert _staff_calls(calls) == []


def test_created_zitadel_and_staff_both_run(db, monkeypatch):
    _env(monkeypatch, zitadel=True)
    from src.services import zitadel_provisioning as zp
    monkeypatch.setattr(zp, "provision_user", lambda *a, **k: "z-123")
    t = _user(db, "t@x.io", role="teacher")
    _seed_created(db, t.id)
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    db.refresh(t)
    assert t.central_auth_user_id == "z-123"       # Zitadel still linked
    assert len(_staff_calls(calls)) == 2           # AND staff provisioned


# --- group.upserted: ensure staff exists before linking ----------------------


def test_group_ensures_teacher_staff_before_delivering(db, monkeypatch):
    _env(monkeypatch)
    _user(db, "t@x.io", role="teacher")
    _seed_group(db, teacher_email="t@x.io", program="ielts")
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    staff = _staff_calls(calls)
    # ielts group -> teacher provisioned as IELTS staff only (not SAT)
    assert [c["url"] for c in staff] == ["https://ielts.example/lms/staff"]
    assert staff[0]["json"]["staff"]["role"] == "teacher"
    # the group itself still fanned out to the group endpoints
    group_urls = [c["url"] for c in calls if not c["url"].endswith("/staff")]
    assert "https://ielts.example/lms/students/groups" in group_urls


def test_group_curator_provisioned_on_sat(db, monkeypatch):
    _env(monkeypatch)
    _user(db, "c@x.io", role="curator")
    _seed_group(db, curator_email="c@x.io", program="nuet")
    calls = _capture(monkeypatch)
    student_sync.drain_outbox(db)
    staff = _staff_calls(calls)
    assert [c["url"] for c in staff] == ["https://sat.example/api/lms/staff"]
    assert staff[0]["json"]["staff"]["role"] == "curator"


def test_group_unknown_email_still_delivers_without_staff(db, monkeypatch):
    _env(monkeypatch)
    _seed_group(db, teacher_email="ghost@x.io", program="ielts")  # no such LMS user
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    assert _staff_calls(calls) == []


def test_group_general_english_provisions_no_staff(db, monkeypatch):
    _env(monkeypatch)
    _user(db, "t@x.io", role="teacher")
    _seed_group(db, teacher_email="t@x.io", program="general_english")
    calls = _capture(monkeypatch)
    result = student_sync.drain_outbox(db)
    assert result["published"] == 1
    assert _staff_calls(calls) == []


def test_group_staff_transient_error_retries_whole_event(db, monkeypatch):
    _env(monkeypatch)
    _user(db, "t@x.io", role="teacher")
    _seed_group(db, teacher_email="t@x.io", program="ielts")
    _capture(monkeypatch, response_for=lambda u, b: _Resp(500) if u.endswith("/staff") else None)
    result = student_sync.drain_outbox(db)
    assert result["retried"] == 1
    assert db.query(StudentSyncOutbox).one().status == "pending"
