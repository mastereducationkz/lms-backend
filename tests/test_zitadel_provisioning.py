"""Zitadel account provisioning (SSO_DESIGN.md §E) — payload shape, idempotency, bulk linking.

All HTTP is mocked; no network. Bulk tests run on in-memory SQLite with just the users table.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.schemas.models  # noqa: F401 - register models
from src.auth.models import UserInDB
from src.services import zitadel_provisioning as zp

BCRYPT = "$2b$12$abcdefghijklmnopqrstuvwxyz012345678901234567890123456"
PBKDF2 = "pbkdf2_sha256$260000$salt$digest"


class _Resp:
    def __init__(self, code, payload=None, text=""):
        self.status_code = code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


class _Client:
    """Scripted httpx.Client stand-in: records posts, returns queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture(autouse=True)
def _pat(monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "test-pat")
    monkeypatch.delenv("ZITADEL_ORG_ID", raising=False)


# --- provision_user ----------------------------------------------------------

def test_disabled_without_pat(monkeypatch):
    monkeypatch.setenv("ZITADEL_PAT", "")
    assert zp.zitadel_enabled() is False
    with pytest.raises(zp.ZitadelError):
        zp.provision_user("a@x.io", "A B", None, client=_Client([]))


def test_imports_bcrypt_hash_with_verified_email():
    c = _Client([_Resp(200, {"userId": "999"})])
    uid = zp.provision_user("Stu@X.io", "Stu Dent", BCRYPT, client=c)
    assert uid == "999"
    body = c.posts[0]["json"]
    assert c.posts[0]["url"].endswith("/management/v1/users/human/_import")
    assert body["userName"] == "stu@x.io"
    assert body["email"] == {"email": "Stu@X.io", "isEmailVerified": True}
    assert body["hashedPassword"] == {"value": BCRYPT, "algorithm": "bcrypt"}
    assert body["passwordChangeRequired"] is False
    assert body["profile"]["firstName"] == "Stu" and body["profile"]["lastName"] == "Dent"
    assert c.posts[0]["headers"]["Authorization"] == "Bearer test-pat"


def test_non_bcrypt_hash_imports_without_password():
    c = _Client([_Resp(200, {"userId": "1000"})])
    zp.provision_user("a@x.io", "A", PBKDF2, client=c)
    body = c.posts[0]["json"]
    assert "hashedPassword" not in body  # pbkdf2 isn't a default Zitadel verifier
    # one-word name degrades to first==last (Zitadel requires both)
    assert body["profile"]["firstName"] == "A" and body["profile"]["lastName"] == "A"


def test_already_exists_resolves_by_email_search():
    c = _Client([
        _Resp(409, text="user already exists"),
        _Resp(200, {"result": [{"id": "777"}]}),  # the _search call
    ])
    assert zp.provision_user("dup@x.io", "Du P", BCRYPT, client=c) == "777"
    assert c.posts[1]["url"].endswith("/management/v1/users/_search")


def test_hard_failure_raises():
    c = _Client([_Resp(500, text="boom")])
    with pytest.raises(zp.ZitadelError):
        zp.provision_user("a@x.io", "A B", BCRYPT, client=c)


def test_rejects_non_email():
    with pytest.raises(zp.ZitadelError):
        zp.provision_user("not-an-email", "A B", None, client=_Client([]))


# --- bulk_import ---------------------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    UserInDB.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _mk(db, email, role="student", cauid=None, active=True, pw=BCRYPT):
    u = UserInDB(email=email, name=f"N {email}", role=role, is_active=active,
                 hashed_password=pw, central_auth_user_id=cauid)
    db.add(u)
    db.commit()
    return u


def test_bulk_links_central_auth_user_id(db, monkeypatch):
    _mk(db, "s1@x.io")
    _mk(db, "t1@x.io", role="teacher", pw=PBKDF2)
    ids = iter(["z1", "z2"])
    monkeypatch.setattr(zp, "provision_user", lambda *a, **k: next(ids))
    monkeypatch.setattr(zp.httpx, "Client", lambda: _Client([]))
    result = zp.bulk_import(db, sleep_seconds=0)
    assert result["imported"] == 2 and result["errors"] == 0
    assert result["with_password"] == 1 and result["without_password"] == 1  # pbkdf2 counted pw-less
    assert {u.central_auth_user_id for u in db.query(UserInDB).all()} == {"z1", "z2"}


def test_bulk_skips_already_linked_and_inactive(db, monkeypatch):
    _mk(db, "linked@x.io", cauid="existing")
    _mk(db, "inactive@x.io", active=False)
    fresh = _mk(db, "fresh@x.io")
    monkeypatch.setattr(zp, "provision_user", lambda *a, **k: "z-new")
    monkeypatch.setattr(zp.httpx, "Client", lambda: _Client([]))
    result = zp.bulk_import(db, sleep_seconds=0)
    assert result["candidates"] == 1 and result["imported"] == 1
    db.refresh(fresh)
    assert fresh.central_auth_user_id == "z-new"


def test_bulk_roles_filter(db, monkeypatch):
    _mk(db, "s@x.io", role="student")
    _mk(db, "t@x.io", role="teacher")
    monkeypatch.setattr(zp, "provision_user", lambda *a, **k: "z")
    monkeypatch.setattr(zp.httpx, "Client", lambda: _Client([]))
    result = zp.bulk_import(db, roles=["teacher"], sleep_seconds=0)
    assert result["candidates"] == 1 and result["imported"] == 1


def test_bulk_dry_run_mutates_nothing(db, monkeypatch):
    _mk(db, "s@x.io")
    called = []
    monkeypatch.setattr(zp, "provision_user", lambda *a, **k: called.append(1) or "z")
    result = zp.bulk_import(db, dry_run=True)
    assert result["candidates"] == 1 and result["imported"] == 0 and not called
    assert db.query(UserInDB).one().central_auth_user_id is None


def test_bulk_one_failure_does_not_stop_the_run(db, monkeypatch):
    _mk(db, "bad@x.io")
    _mk(db, "good@x.io")
    def _prov(email, *a, **k):
        if email == "bad@x.io":
            raise zp.ZitadelError("nope")
        return "z-good"
    monkeypatch.setattr(zp, "provision_user", _prov)
    monkeypatch.setattr(zp.httpx, "Client", lambda: _Client([]))
    result = zp.bulk_import(db, sleep_seconds=0)
    assert result["errors"] == 1 and result["imported"] == 1
    good = db.query(UserInDB).filter_by(email="good@x.io").one()
    assert good.central_auth_user_id == "z-good"
