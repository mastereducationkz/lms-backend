"""Single-student provisioning helpers behind the admin «provision to SAT/IELTS» button
(src/services/sync_provision_gaps.py). Mirrors test_sync_provision_gaps: in-memory SQLite, mocked
httpx.post. Covers the create-or-fetch call, SAT product derivation from the student's groups, and
the membership re-emit touch."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.services import sync_provision_gaps as spg


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


class _Resp:
    def __init__(self, code, json_body=None, text=""):
        self.status_code = code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


_uid = iter(range(1, 10_000))
_gid = iter(range(1, 10_000))


def _student(db, email="s@x.io", name="Stu"):
    u = UserInDB(id=next(_uid), email=email, name=name, role="student", hashed_password="x", is_active=True)
    db.add(u)
    db.commit()
    return u


def _group(db, program, student_id):
    g = Group(id=next(_gid), name=f"{program}-grp", program_type=program)
    db.add(g)
    db.commit()
    db.add(GroupStudent(group_id=g.id, student_id=student_id))
    db.commit()
    return g


def _env(monkeypatch):
    monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/lms")
    monkeypatch.setenv("IELTS_API_KEY", "kielts")
    monkeypatch.setenv("SAT_SYNC_URL", "https://sat.example/api/lms")
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")


# --- provision_student_account -----------------------------------------------


def test_provision_ielts_hits_students_endpoint(monkeypatch):
    _env(monkeypatch)
    calls = []
    monkeypatch.setattr(spg.httpx, "post",
                        lambda url, json, headers, timeout: calls.append({"url": url, "json": json}) or _Resp(201, {"created": True}))
    outcome, _ = spg.provision_student_account("s@x.io", "Stu", "ielts")
    assert outcome == "created"
    assert calls[0]["url"] == "https://ielts.example/lms/students/"
    assert calls[0]["json"]["send_credentials"] is False  # SSO students: never emailed


def test_provision_sat_sends_product(monkeypatch):
    _env(monkeypatch)
    calls = []
    monkeypatch.setattr(spg.httpx, "post",
                        lambda url, json, headers, timeout: calls.append({"url": url, "json": json}) or _Resp(200, {"created": False}))
    outcome, _ = spg.provision_student_account("s@x.io", "Stu", "sat", "NUET")
    assert outcome == "exists"
    assert calls[0]["json"]["product"] == "NUET"
    assert calls[0]["json"]["send_credentials"] is False


def test_provision_error_on_5xx(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(spg.httpx, "post", lambda url, json, headers, timeout: _Resp(500, text="boom"))
    outcome, detail = spg.provision_student_account("s@x.io", "Stu", "ielts")
    assert outcome == "error"
    assert "500" in detail


# --- sat_product_for_student -------------------------------------------------


def test_sat_product_derived_from_groups(db):
    s = _student(db)
    _group(db, "sat", s.id)
    _group(db, "nuet", s.id)
    assert spg.sat_product_for_student(db, s.id) == "BOTH"


def test_sat_product_single_program(db):
    s = _student(db)
    _group(db, "nuet", s.id)
    assert spg.sat_product_for_student(db, s.id) == "NUET"


def test_sat_product_defaults_to_sat_when_no_group(db):
    s = _student(db)
    assert spg.sat_product_for_student(db, s.id) == "SAT"


# --- reemit_student_memberships ----------------------------------------------


def test_reemit_touches_only_platform_program_groups(db):
    s = _student(db)
    _group(db, "ielts", s.id)
    _group(db, "sat", s.id)
    _group(db, "nuet", s.id)
    # SAT platform owns sat+nuet -> 2 rows touched; ielts group left alone.
    assert spg.reemit_student_memberships(db, s.id, "sat") == 2
    # IELTS platform owns ielts -> 1 row.
    assert spg.reemit_student_memberships(db, s.id, "ielts") == 1


def test_reemit_unknown_platform_is_noop(db):
    s = _student(db)
    _group(db, "ielts", s.id)
    assert spg.reemit_student_memberships(db, s.id, "nope") == 0


# --- platform_configured -----------------------------------------------------


def test_platform_configured(monkeypatch):
    _env(monkeypatch)
    assert spg.platform_configured("ielts") is True
    assert spg.platform_configured("sat") is True
    monkeypatch.delenv("IELTS_API_KEY", raising=False)
    assert spg.platform_configured("ielts") is False
