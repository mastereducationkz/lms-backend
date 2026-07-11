"""Gap-student provisioning CLI (src/services/sync_provision_gaps.py).

Mirrors the drainer tests in test_student_sync.py: seed dead-lettered (status='failed')
member.upserted outbox rows on an in-memory SQLite DB, mock httpx.post, and assert the tool
targets the right endpoint / product per program and tallies created/exists/error. --dry-run must
make zero HTTP calls.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.courses.models import StudentSyncOutbox
from src.services import sync_provision_gaps as spg


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    StudentSyncOutbox.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed_failed(db, eid, email, name, program, *, status="failed", event_type="member.upserted"):
    """Seed one dead-lettered member.upserted row shaped like the p8 trigger's payload."""
    payload = {
        "event_id": eid,
        "event_type": event_type,
        "source": "lms_db_trigger",
        "lms_group_id": 9,
        "group_name": f"{program}-grp",
        "program_type": program,
        "student": {"lms_student_id": 1, "email": email, "central_auth_user_id": "c", "name": name},
    }
    row = StudentSyncOutbox(
        event_id=eid, event_type=event_type, payload=payload, status=status, last_error="404 not found"
    )
    db.add(row)
    db.commit()
    return row


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

    monkeypatch.setattr(spg.httpx, "post", fake_post)
    return calls


def _env(monkeypatch):
    monkeypatch.setenv("IELTS_SYNC_URL", "https://ielts.example/lms")
    monkeypatch.setenv("IELTS_API_KEY", "kielts")
    monkeypatch.setenv("SAT_SYNC_URL", "https://sat.example/api/lms")
    monkeypatch.setenv("MASTEREDU_API_KEY", "ksat")


# --- gap discovery ---------------------------------------------------------


def test_find_gaps_ignores_non_failed_and_non_member_rows(db):
    _seed_failed(db, "m1", "a@x.io", "A", "ielts")
    _seed_failed(db, "m2", "b@x.io", "B", "sat", status="pending")  # not dead-lettered
    _seed_failed(db, "m3", "c@x.io", "C", "sat", event_type="group.upserted")  # wrong event type
    gaps = spg.find_gap_students(db)
    assert [g.email for g in gaps] == ["a@x.io"]


def test_find_gaps_dedups_email_and_lowercases(db):
    _seed_failed(db, "m1", "Dup@X.io", "Dup", "sat")
    _seed_failed(db, "m2", "dup@x.io", "Dup", "sat")  # same student, second failed group
    gaps = spg.find_gap_students(db)
    assert len(gaps) == 1
    assert gaps[0].email == "dup@x.io"
    assert gaps[0].platform == "sat"


def test_find_gaps_skips_rows_without_email(db):
    _seed_failed(db, "m1", "", None, "sat")
    assert spg.find_gap_students(db) == []


def test_sat_product_derivation(db):
    _seed_failed(db, "s1", "sat@x.io", "S", "sat")
    _seed_failed(db, "n1", "nuet@x.io", "N", "nuet")
    _seed_failed(db, "b1", "both@x.io", "B", "sat")
    _seed_failed(db, "b2", "both@x.io", "B", "nuet")
    by_email = {g.email: g for g in spg.find_gap_students(db)}
    assert by_email["sat@x.io"].product == "SAT"
    assert by_email["nuet@x.io"].product == "NUET"
    assert by_email["both@x.io"].product == "BOTH"


def test_student_gap_on_both_platforms_yields_two_gaps(db):
    _seed_failed(db, "i1", "multi@x.io", "M", "ielts")
    _seed_failed(db, "s1", "multi@x.io", "M", "sat")
    gaps = [g for g in spg.find_gap_students(db) if g.email == "multi@x.io"]
    assert {g.platform for g in gaps} == {"ielts", "sat"}


def test_unknown_program_has_no_target(db):
    _seed_failed(db, "g1", "ge@x.io", "GE", "general_english")
    assert spg.find_gap_students(db) == []


# --- provisioning: endpoint + product routing ------------------------------


def test_ielts_targets_students_slash_with_no_credentials_email(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "i1", "i@x.io", "Ivy", "ielts")
    calls = _capture(monkeypatch)
    result = spg.provision_gaps(db, platform="ielts")
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == "https://ielts.example/lms/students/"
    assert c["key"] == "kielts"
    assert c["timeout"] == 15.0
    assert c["json"] == {"email": "i@x.io", "full_name": "Ivy", "send_credentials": False}
    assert "product" not in c["json"]
    assert result["created"] == 1


def test_sat_targets_students_with_product_and_no_credentials_email(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "s1", "s@x.io", "Sam", "nuet")
    calls = _capture(monkeypatch)
    result = spg.provision_gaps(db, platform="sat")
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == "https://sat.example/api/lms/students"
    assert c["key"] == "ksat"
    assert c["json"] == {
        "email": "s@x.io",
        "full_name": "Sam",
        "product": "NUET",
        "send_credentials": False,
    }
    assert result["created"] == 1


def test_sat_both_product_when_student_has_sat_and_nuet_rows(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "b1", "b@x.io", "Bo", "sat")
    _seed_failed(db, "b2", "b@x.io", "Bo", "nuet")
    calls = _capture(monkeypatch)
    spg.provision_gaps(db, platform="sat")
    assert len(calls) == 1
    assert calls[0]["json"]["product"] == "BOTH"


def test_platform_all_hits_both_endpoints(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "i1", "i@x.io", "I", "ielts")
    _seed_failed(db, "s1", "s@x.io", "S", "sat")
    calls = _capture(monkeypatch)
    result = spg.provision_gaps(db, platform="all")
    urls = sorted(c["url"] for c in calls)
    assert urls == ["https://ielts.example/lms/students/", "https://sat.example/api/lms/students"]
    assert result["total"] == 2
    assert result["by_platform"]["ielts"]["total"] == 1
    assert result["by_platform"]["sat"]["total"] == 1


# --- tally: created / exists / error ---------------------------------------


def test_tallies_created_exists_and_error(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "c1", "created@x.io", "C", "sat")
    _seed_failed(db, "e1", "exists@x.io", "E", "sat")
    _seed_failed(db, "x1", "error@x.io", "X", "sat")

    def resp(url, body):
        if body["email"] == "created@x.io":
            return _Resp(201, {"created": True})
        if body["email"] == "exists@x.io":
            return _Resp(200, {"created": False})
        return _Resp(422, text="Invalid email address")

    _capture(monkeypatch, response_for=resp)
    result = spg.provision_gaps(db, platform="sat")
    assert result["created"] == 1
    assert result["exists"] == 1
    assert result["error"] == 1
    assert result["by_platform"]["sat"] == {"total": 3, "created": 1, "exists": 1, "error": 1}
    assert any("422" in e for e in result["errors"])


def test_200_created_false_counts_as_exists(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "e1", "e@x.io", "E", "ielts")
    _capture(monkeypatch, response_for=lambda u, b: _Resp(200, {"created": False}))
    result = spg.provision_gaps(db, platform="ielts")
    assert result == {**result, "created": 0, "exists": 1, "error": 0}


def test_transport_error_is_tallied_not_raised(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "t1", "t@x.io", "T", "sat")

    def boom(url, json, headers, timeout):
        raise spg.httpx.ConnectError("no route")

    monkeypatch.setattr(spg.httpx, "post", boom)
    result = spg.provision_gaps(db, platform="sat")
    assert result["error"] == 1
    assert any("transport error" in e for e in result["errors"])


def test_missing_api_key_is_error_without_calling(db, monkeypatch):
    monkeypatch.setenv("SAT_SYNC_URL", "https://sat.example/api/lms")
    monkeypatch.delenv("MASTEREDU_API_KEY", raising=False)
    _seed_failed(db, "s1", "s@x.io", "S", "sat")
    calls = _capture(monkeypatch)
    result = spg.provision_gaps(db, platform="sat")
    assert calls == []  # never attempted without a key
    assert result["error"] == 1
    assert any("api key not configured" in e for e in result["errors"])


# --- dry-run + limit -------------------------------------------------------


def test_dry_run_makes_no_calls_and_lists_targets(db, monkeypatch):
    _env(monkeypatch)
    _seed_failed(db, "i1", "i@x.io", "I", "ielts")
    _seed_failed(db, "s1", "s@x.io", "S", "nuet")
    calls = _capture(monkeypatch)
    result = spg.provision_gaps(db, platform="all", dry_run=True)
    assert calls == []  # NOTHING is provisioned
    assert result["dry_run"] is True
    assert result["total"] == 2
    listed = {r["email"]: r for r in result["would_provision"]}
    assert listed["i@x.io"]["platform"] == "ielts"
    assert listed["s@x.io"]["product"] == "NUET"


def test_limit_caps_the_number_processed(db, monkeypatch):
    _env(monkeypatch)
    for i in range(5):
        _seed_failed(db, f"s{i}", f"s{i}@x.io", f"S{i}", "sat")
    calls = _capture(monkeypatch)
    result = spg.provision_gaps(db, platform="sat", limit=2)
    assert len(calls) == 2
    assert result["total"] == 2


def test_invalid_platform_raises(db):
    with pytest.raises(ValueError):
        spg.provision_gaps(db, platform="nope")
