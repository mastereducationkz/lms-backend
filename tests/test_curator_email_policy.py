"""The LMS refuses curator operational email on its own authority.

The CRM already clears the flag before calling. This is the independent second check, and it
exists because the first one lives in another repository behind an HTTP boundary and can be
redeployed without this one: an older CRM build, a replayed request, or any future direct
caller of `/internal/crm/curator/notify` would otherwise sail straight past it.

Authentication and account-recovery mail is deliberately *not* covered — it is composed
elsewhere and never reaches `send_curator_email`.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.schemas.models import UserInDB


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
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close()
        trans.rollback()


def _user(db, role: str) -> UserInDB:
    user = UserInDB(
        email=f"{role}-{datetime.utcnow().timestamp()}@test.local",
        name=role, role=role, hashed_password="x", is_active=True,
    )
    db.add(user)
    db.flush()
    return user


@pytest.mark.parametrize("role", ["curator", "head_curator"])
def test_a_curator_is_recognised_by_id_and_by_address(db, role):
    from src.curator.email_policy import is_curator, is_curator_email

    user = _user(db, role)

    assert is_curator(db, user.id) is True
    assert is_curator_email(db, user.email) is True


@pytest.mark.parametrize("role", ["teacher", "student", "admin", "head_teacher", "parent"])
def test_everybody_else_keeps_ordinary_email_behaviour(db, role):
    from src.curator.email_policy import is_curator, is_curator_email

    user = _user(db, role)

    assert is_curator(db, user.id) is False
    assert is_curator_email(db, user.email) is False


def test_unknown_addresses_and_empty_input_are_not_curators(db):
    from src.curator.email_policy import is_curator, is_curator_email

    assert is_curator(db, None) is False
    assert is_curator_email(db, None) is False
    assert is_curator_email(db, "") is False
    assert is_curator_email(db, "nobody@nowhere.local") is False


@pytest.fixture
def own_session(db, monkeypatch):
    """`send_curator_email` opens its own session — it is called after the caller's commit,
    which is deliberate. Point that factory at the test's savepoint session so the fixture's
    uncommitted rows are visible to it."""
    import src.config

    monkeypatch.setattr(src.config, "SessionLocal", lambda: _NoCloseSession(db))
    return db


class _NoCloseSession:
    """Delegates to the test session but ignores `close()`, which the caller always calls."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        return None


def test_send_curator_email_withholds_operational_mail_to_a_curator(own_session, monkeypatch):
    """The last line of defence: every composed curator email passes through here."""
    from src.curator import notifications

    curator = _user(own_session, "curator")
    own_session.flush()

    sent: list = []
    monkeypatch.setattr(notifications, "_send_email_safe",
                        lambda *a, **k: sent.append(a) or True)

    result = notifications.send_curator_email(
        curator.email, "Тема", ["строка"], link="https://crm.local/x"
    )

    assert result is False
    assert sent == [], "nothing reached the sender"


def test_send_curator_email_still_works_for_a_non_curator(own_session, monkeypatch):
    from src.curator import notifications

    teacher = _user(own_session, "teacher")
    own_session.flush()

    sent: list = []
    monkeypatch.setattr(notifications, "_send_email_safe",
                        lambda *a, **k: sent.append(a) or True)

    result = notifications.send_curator_email(teacher.email, "Тема", ["строка"])

    assert result is True
    assert len(sent) == 1


def test_a_policy_lookup_failure_withholds_rather_than_delivers(db, monkeypatch):
    """Fail closed. An unavailable check must not become a delivered curator email."""
    from src.curator import notifications

    monkeypatch.setattr(
        "src.curator.email_policy.is_curator_email",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    sent: list = []
    monkeypatch.setattr(notifications, "_send_email_safe",
                        lambda *a, **k: sent.append(a) or True)

    assert notifications.send_curator_email("someone@test.local", "s", ["l"]) is False
    assert sent == []
