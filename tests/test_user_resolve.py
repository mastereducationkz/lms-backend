"""resolve_user_by_payload (SSO Phase 2): stable central-auth-id linkage + backfill.

Uses an in-memory SQLite DB with the real models so the query paths are exercised
for real (no mocks)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import the full model graph so create_all builds every table the relationships need.
import src.schemas.models  # noqa: F401
from src.models.base import Base
from src.schemas.models import UserInDB
from src.auth.user_resolve import resolve_user_by_payload


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # Only the users table is needed; the full metadata includes Postgres-only column
    # types in unrelated tables that SQLite can't compile.
    UserInDB.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _mk(db, *, email, caid=None, active=True):
    u = UserInDB(
        email=email, name="Test", hashed_password="x", role="teacher",
        is_active=active, central_auth_user_id=caid,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_legacy_payload_resolves_by_email(db):
    u = _mk(db, email="a@b.com")
    got = resolve_user_by_payload(db, {"sub": "A@B.com"})  # no oidc flag
    assert got is not None and got.id == u.id


def test_legacy_unknown_email_returns_none(db):
    _mk(db, email="a@b.com")
    assert resolve_user_by_payload(db, {"sub": "nobody@x.com"}) is None


def test_oidc_first_login_matches_email_and_backfills_id(db):
    u = _mk(db, email="teacher@x.com", caid=None)
    payload = {"sub": "teacher@x.com", "oidc": True, "central_auth_user_id": "idp-sub-123"}
    got = resolve_user_by_payload(db, payload)
    assert got is not None and got.id == u.id
    # id backfilled and persisted
    db.refresh(u)
    assert u.central_auth_user_id == "idp-sub-123"


def test_oidc_matches_by_stable_id_even_if_email_changed(db):
    # User already linked; their email in the token differs (e.g. changed in the IdP).
    u = _mk(db, email="old@x.com", caid="idp-sub-123")
    payload = {"sub": "totally-different@x.com", "oidc": True, "central_auth_user_id": "idp-sub-123"}
    got = resolve_user_by_payload(db, payload)
    assert got is not None and got.id == u.id  # matched by id, not email


def test_oidc_id_takes_precedence_over_email(db):
    linked = _mk(db, email="linked@x.com", caid="idp-sub-1")
    _other = _mk(db, email="other@x.com", caid=None)
    # Token id points at `linked`, but sub-email matches `other`. Id must win.
    payload = {"sub": "other@x.com", "oidc": True, "central_auth_user_id": "idp-sub-1"}
    got = resolve_user_by_payload(db, payload)
    assert got.id == linked.id


def test_oidc_no_match_returns_none(db):
    _mk(db, email="a@b.com", caid="idp-1")
    payload = {"sub": "ghost@x.com", "oidc": True, "central_auth_user_id": "idp-ghost"}
    assert resolve_user_by_payload(db, payload) is None
