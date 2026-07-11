"""Staff-provisioning CLI (src/services/sync_provision_staff.py).

Mirrors test_sync_provision_gaps.py: seed groups + members + staff users on an in-memory SQLite
DB, mock httpx.post, and assert the tool targets the right platform + role per group program and
tallies created/exists/error. --dry-run must make zero HTTP calls; test emails are skipped.

The Group model carries a Postgres JSONB column (schedule_config); we compile JSONB -> JSON on the
SQLite dialect so the three tables can be created in-memory (the tool never touches that column).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.services import sync_provision_staff as sps


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(element, compiler, **kw):  # pragma: no cover - dialect glue
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (UserInDB, Group, GroupStudent):
        model.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


_uid = iter(range(1, 10_000))
_gid = iter(range(1, 10_000))


def _user(db, email, name="Staff", role="teacher"):
    u = UserInDB(
        id=next(_uid),
        email=email,
        name=name,
        role=role,
        hashed_password="x",
        is_active=True,
    )
    db.add(u)
    db.commit()
    return u


def _group(db, program, *, teacher_id=None, curator_id=None, with_member=True):
    g = Group(
        id=next(_gid),
        name=f"{program}-grp",
        program_type=program,
        teacher_id=teacher_id,
        curator_id=curator_id,
    )
    db.add(g)
    db.commit()
    if with_member:
        # a group with members is what makes its staff eligible for provisioning
        student = _user(db, f"stu{g.id}@x.io", name="Stu", role="student")
        db.add(GroupStudent(group_id=g.id, student_id=student.id))
        db.commit()
    return g


class _Resp:
    def __init__(self, code, json_body=None, text=""):
        self.status_code = code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


def _capture(monkeypatch, response_for=None):
    """Patch httpx.post to record calls; response_for(url, body) -> _Resp (default 201 created)."""
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "key": headers.get("X-API-Key"), "timeout": timeout})
        if response_for is not None:
            return response_for(url, json)
        return _Resp(201, {"created": True})

    monkeypatch.setattr(sps.httpx, "post", fake_post)
    return calls


def _env(monkeypatch):
    monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/lms")
    monkeypatch.setenv("IELTS_API_KEY", "kielts")
    monkeypatch.setenv("SAT_SYNC_URL", "https://sat.example/api/lms")
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")


# --- staff discovery -------------------------------------------------------


def test_discovers_teacher_of_sat_group_as_sat_teacher(db):
    t = _user(db, "t@x.io", name="Ms T", role="teacher")
    _group(db, "sat", teacher_id=t.id)
    staff = sps.find_staff_to_provision(db)
    assert len(staff) == 1
    s = staff[0]
    assert s.email == "t@x.io"
    assert s.name == "Ms T"
    assert s.role == "teacher"
    assert s.platform == "sat"
    assert s.lms_user_id == t.id


def test_ielts_group_routes_to_ielts_platform(db):
    c = _user(db, "c@x.io", role="curator")
    _group(db, "ielts", curator_id=c.id)
    staff = sps.find_staff_to_provision(db)
    assert [(s.platform, s.role) for s in staff] == [("ielts", "curator")]


def test_nuet_group_routes_to_sat_platform(db):
    t = _user(db, "t@x.io", role="head_teacher")
    _group(db, "nuet", teacher_id=t.id)
    staff = sps.find_staff_to_provision(db)
    assert staff[0].platform == "sat"
    assert staff[0].role == "teacher"  # head_teacher family -> platform "teacher"


def test_curator_id_slot_maps_to_curator_family(db):
    # head_curator sitting in the curator_id slot -> platform role "curator".
    c = _user(db, "hc@x.io", role="head_curator")
    _group(db, "sat", curator_id=c.id)
    staff = sps.find_staff_to_provision(db)
    assert staff[0].role == "curator"


def test_group_without_members_is_ignored(db):
    t = _user(db, "t@x.io", role="teacher")
    _group(db, "sat", teacher_id=t.id, with_member=False)
    assert sps.find_staff_to_provision(db) == []


def test_same_user_teacher_and_curator_on_different_platforms_yields_two(db):
    u = _user(db, "both@x.io", name="Both", role="teacher")
    _group(db, "sat", teacher_id=u.id)  # teacher on SAT
    _group(db, "ielts", curator_id=u.id)  # curator on IELTS
    staff = {(s.platform, s.role) for s in sps.find_staff_to_provision(db)}
    assert staff == {("sat", "teacher"), ("ielts", "curator")}


def test_same_teacher_two_sat_groups_deduped(db):
    t = _user(db, "t@x.io", role="teacher")
    _group(db, "sat", teacher_id=t.id)
    _group(db, "nuet", teacher_id=t.id)  # both SAT-family, same platform+role
    staff = sps.find_staff_to_provision(db)
    assert len(staff) == 1


def test_general_english_group_has_no_target(db):
    t = _user(db, "t@x.io", role="teacher")
    _group(db, "general_english", teacher_id=t.id)
    assert sps.find_staff_to_provision(db) == []


def test_test_and_internal_emails_are_skipped(db):
    lms = _user(db, "someone@lms.com", role="teacher")
    testy = _user(db, "test@real.io", role="curator")
    _group(db, "sat", teacher_id=lms.id)
    _group(db, "ielts", curator_id=testy.id)
    assert sps.find_staff_to_provision(db) == []


def test_non_staff_role_in_teacher_slot_is_skipped(db):
    # A student accidentally sitting in teacher_id must not be provisioned as staff.
    stu = _user(db, "oops@x.io", role="student")
    _group(db, "sat", teacher_id=stu.id)
    assert sps.find_staff_to_provision(db) == []


def test_null_email_is_skipped(db):
    # email is NOT NULL in the model, but an empty string must be filtered out.
    t = _user(db, "", role="teacher")
    _group(db, "sat", teacher_id=t.id)
    assert sps.find_staff_to_provision(db) == []


# --- provisioning: endpoint + role routing ---------------------------------


def test_sat_targets_staff_endpoint_with_role_and_envelope(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "t@x.io", name="Ms T", role="teacher")
    _group(db, "sat", teacher_id=t.id)
    calls = _capture(monkeypatch)
    result = sps.provision_staff(db, platform="sat")
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == "https://sat.example/api/lms/staff"
    assert c["key"] == "ksat"
    assert c["timeout"] == 15.0
    body = c["json"]
    assert body["event_type"] == "staff.upserted"
    assert body["source"] == "lms_provision_staff"
    assert body["event_id"]  # a generated uuid
    assert body["staff"] == {
        "lms_user_id": t.id,
        "email": "t@x.io",
        "name": "Ms T",
        "role": "teacher",
    }
    assert result["created"] == 1


def test_ielts_targets_staff_endpoint(db, monkeypatch):
    _env(monkeypatch)
    c = _user(db, "c@x.io", name="Mr C", role="curator")
    _group(db, "ielts", curator_id=c.id)
    calls = _capture(monkeypatch)
    result = sps.provision_staff(db, platform="ielts")
    assert len(calls) == 1
    assert calls[0]["url"] == "https://ielts.example/lms/staff"
    assert calls[0]["key"] == "kielts"
    assert calls[0]["json"]["staff"]["role"] == "curator"
    assert result["created"] == 1


def test_platform_all_hits_both_endpoints(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "t@x.io", role="teacher")
    c = _user(db, "c@x.io", role="curator")
    _group(db, "sat", teacher_id=t.id)
    _group(db, "ielts", curator_id=c.id)
    calls = _capture(monkeypatch)
    result = sps.provision_staff(db, platform="all")
    urls = sorted(x["url"] for x in calls)
    assert urls == ["https://ielts.example/lms/staff", "https://sat.example/api/lms/staff"]
    assert result["total"] == 2
    assert result["by_platform"]["ielts"]["total"] == 1
    assert result["by_platform"]["sat"]["total"] == 1


# --- tally: created / exists / error ---------------------------------------


def test_tallies_created_exists_and_error(db, monkeypatch):
    _env(monkeypatch)
    created = _user(db, "created@x.io", role="teacher")
    exists = _user(db, "exists@x.io", role="teacher")
    error = _user(db, "error@x.io", role="teacher")
    _group(db, "sat", teacher_id=created.id)
    _group(db, "sat", teacher_id=exists.id)
    _group(db, "sat", teacher_id=error.id)

    def resp(url, body):
        email = body["staff"]["email"]
        if email == "created@x.io":
            return _Resp(201, {"created": True})
        if email == "exists@x.io":
            return _Resp(200, {"created": False, "reason": "exists"})
        return _Resp(400, text="role not provisioned")

    _capture(monkeypatch, response_for=resp)
    result = sps.provision_staff(db, platform="sat")
    assert result["created"] == 1
    assert result["exists"] == 1
    assert result["error"] == 1
    assert result["by_platform"]["sat"] == {"total": 3, "created": 1, "exists": 1, "error": 1}
    assert any("400" in e for e in result["errors"])


def test_200_created_false_counts_as_exists(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "e@x.io", role="teacher")
    _group(db, "ielts", teacher_id=t.id)
    _capture(monkeypatch, response_for=lambda u, b: _Resp(200, {"created": False}))
    result = sps.provision_staff(db, platform="ielts")
    assert result["created"] == 0
    assert result["exists"] == 1
    assert result["error"] == 0


def test_transport_error_is_tallied_not_raised(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "t@x.io", role="teacher")
    _group(db, "sat", teacher_id=t.id)

    def boom(url, json, headers, timeout):
        raise sps.httpx.ConnectError("no route")

    monkeypatch.setattr(sps.httpx, "post", boom)
    result = sps.provision_staff(db, platform="sat")
    assert result["error"] == 1
    assert any("transport error" in e for e in result["errors"])


def test_missing_api_key_is_error_without_calling(db, monkeypatch):
    monkeypatch.setenv("SAT_SYNC_URL", "https://sat.example/api/lms")
    monkeypatch.delenv("MASTEREDU_API_KEY", raising=False)
    t = _user(db, "t@x.io", role="teacher")
    _group(db, "sat", teacher_id=t.id)
    calls = _capture(monkeypatch)
    result = sps.provision_staff(db, platform="sat")
    assert calls == []  # never attempted without a key
    assert result["error"] == 1
    assert any("api key not configured" in e for e in result["errors"])


# --- dry-run + limit -------------------------------------------------------


def test_dry_run_makes_no_calls_and_lists_targets(db, monkeypatch):
    _env(monkeypatch)
    t = _user(db, "t@x.io", name="Ms T", role="teacher")
    c = _user(db, "c@x.io", role="curator")
    _group(db, "sat", teacher_id=t.id)
    _group(db, "ielts", curator_id=c.id)
    calls = _capture(monkeypatch)
    result = sps.provision_staff(db, platform="all", dry_run=True)
    assert calls == []  # NOTHING is provisioned
    assert result["dry_run"] is True
    assert result["total"] == 2
    listed = {r["email"]: r for r in result["would_provision"]}
    assert listed["t@x.io"]["platform"] == "sat"
    assert listed["t@x.io"]["role"] == "teacher"
    assert listed["c@x.io"]["platform"] == "ielts"
    assert listed["c@x.io"]["role"] == "curator"


def test_limit_caps_the_number_processed(db, monkeypatch):
    _env(monkeypatch)
    for i in range(5):
        t = _user(db, f"t{i}@x.io", role="teacher")
        _group(db, "sat", teacher_id=t.id)
    calls = _capture(monkeypatch)
    result = sps.provision_staff(db, platform="sat", limit=2)
    assert len(calls) == 2
    assert result["total"] == 2


def test_invalid_platform_raises(db):
    with pytest.raises(ValueError):
        sps.provision_staff(db, platform="nope")
