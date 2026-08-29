"""Per-device refresh sessions: multi-device coexistence, rotation grace, legacy fallback.

The old single users.refresh_token slot meant any refresh on one device logged every
other device out — the recurring "calendar is empty after refresh" reports. These
tests pin the new contract.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException, Response

from src.schemas.models import UserInDB  # noqa: F401  (import-order guard: shim first)
from src.auth.models import RefreshSession
from src.auth.routes.auth import (
    RefreshTokenRequest,
    _revoke_refresh_sessions,
    _start_refresh_session,
    refresh_token as refresh_endpoint,
)
from src.utils.auth_utils import create_refresh_token


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture
def user(db):
    u = UserInDB(email="refresh.race@test.local", name="Refresh Race",
                 hashed_password="x", role="teacher", is_active=True)
    db.add(u)
    db.flush()
    return u


def _mint(db, user):
    import uuid
    token = create_refresh_token(data={"sub": user.email, "jti": uuid.uuid4().hex})
    _start_refresh_session(db, user, token)
    db.flush()
    return token


def _refresh(db, token):
    return refresh_endpoint(RefreshTokenRequest(refresh_token=token), Response(), db)


def test_two_devices_do_not_log_each_other_out(db, user):
    phone = _mint(db, user)
    desktop = _mint(db, user)

    phone_new = _refresh(db, phone)["refresh_token"]
    assert phone_new != phone

    # The desktop's chain must survive the phone's rotation.
    desktop_new = _refresh(db, desktop)["refresh_token"]
    assert desktop_new != desktop
    # And both rotated chains keep working independently.
    assert _refresh(db, phone_new)["refresh_token"] != phone_new
    assert _refresh(db, desktop_new)["refresh_token"] != desktop_new


def test_raced_refresh_within_grace_gets_current_token(db, user):
    token = _mint(db, user)
    rotated = _refresh(db, token)["refresh_token"]

    # A second tab replays the OLD token moments later: not a 401, it lands on
    # the chain's current token.
    raced = _refresh(db, token)
    assert raced["refresh_token"] == rotated


def test_replay_beyond_grace_is_rejected(db, user):
    token = _mint(db, user)
    rotated = _refresh(db, token)["refresh_token"]

    session = db.query(RefreshSession).filter(RefreshSession.token == rotated).one()
    session.rotated_at = datetime.utcnow() - timedelta(seconds=300)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        _refresh(db, token)
    assert exc.value.status_code == 401


def test_legacy_column_token_migrates_into_a_session(db, user):
    legacy = create_refresh_token(data={"sub": user.email, "jti": __import__("uuid").uuid4().hex})
    user.refresh_token = legacy  # pre-deploy state: no session row
    db.flush()

    result = _refresh(db, legacy)
    assert result["refresh_token"] != legacy
    assert db.query(RefreshSession).filter(
        RefreshSession.token == result["refresh_token"]).count() == 1


def test_revocation_kills_every_chain(db, user):
    phone = _mint(db, user)
    desktop = _mint(db, user)
    _revoke_refresh_sessions(db, user)
    db.flush()

    for token in (phone, desktop):
        with pytest.raises(HTTPException) as exc:
            _refresh(db, token)
        assert exc.value.status_code == 401
