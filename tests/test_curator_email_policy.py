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


# --- the single funnel every LMS email passes through ------------------------------------
#
# Guarding each composer is not enough: there are at least six paths that build curator mail,
# one of them assembles its own HTML and never calls the shared helper, and the next one
# nobody has written yet would be seventh. `EmailService.send_email` is the one chokepoint.


@pytest.fixture
def service(db, monkeypatch):
    """A configured service whose HTTP calls are captured, pointed at the test session."""
    import src.config
    from src.services import email_service as module

    monkeypatch.setattr(src.config, "SessionLocal", lambda: _NoCloseSession(db))
    calls: list = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "msg-1"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        module.requests, "post",
        lambda url, json=None, headers=None, timeout=None: (calls.append(json), _Resp())[1],
    )
    svc = module.EmailService()
    svc.api_key = "re_test_only_not_a_real_key"
    return svc, calls


@pytest.mark.parametrize("event_type", ["curator_transfer", "lesson_change", "curator_notify"])
def test_operational_mail_to_a_curator_never_leaves_the_funnel(db, service, event_type):
    """`curator_transfer` and `lesson_change` are the two types production actually
    delivered to curators — 7 and 2 messages respectively."""
    svc, calls = service
    curator = _user(db, "curator")
    db.flush()

    result = svc.send_email([curator.email], "Тема", "<p>текст</p>", event_type=event_type)

    assert result is None
    assert calls == [], "nothing was handed to the provider"


@pytest.mark.parametrize("event_type", ["invite", "trial_invite", "password_reset", "password_changed"])
def test_a_curator_still_receives_identity_and_recovery_mail(db, service, event_type):
    """Allow-list, not block-list: there will never be a new kind of password reset, but
    there will be new operational event types, and forgetting one must withhold rather than
    leak."""
    svc, calls = service
    curator = _user(db, "curator")
    db.flush()

    svc.send_email([curator.email], "Доступ", "<p>ссылка</p>", event_type=event_type)

    assert len(calls) == 1
    assert calls[0]["to"] == [curator.email]


def test_a_mixed_recipient_list_drops_only_the_curators(db, service):
    """A digest addressed to a teacher and a curator still reaches the teacher."""
    svc, calls = service
    curator = _user(db, "curator")
    teacher = _user(db, "teacher")
    db.flush()

    svc.send_email(
        [curator.email, teacher.email], "Тема", "<p>x</p>", event_type="lesson_change"
    )

    assert len(calls) == 1
    assert calls[0]["to"] == [teacher.email], "the curator was removed, the teacher kept"


def test_non_curators_are_completely_unaffected(db, service):
    svc, calls = service
    for role in ("teacher", "student", "parent", "admin"):
        user = _user(db, role)
        db.flush()
        svc.send_email([user.email], "Тема", "<p>x</p>", event_type="lesson_change")
    assert len(calls) == 4


def test_a_broken_policy_check_withholds_every_recipient(db, service, monkeypatch):
    """A check that is present but broken must not deliver."""
    svc, calls = service
    monkeypatch.setattr(
        "src.curator.email_policy.curator_user_ids",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("something is wrong")),
    )
    teacher = _user(db, "teacher")
    db.flush()

    assert svc.send_email([teacher.email], "Тема", "<p>x</p>", event_type="lesson_change") is None
    assert calls == []


def test_an_unreachable_database_does_not_stop_the_whole_school_s_mail(db, service, monkeypatch):
    """The other failure mode, handled the other way round on purpose.

    With no database there is no `users` table to hold a curator, so the question is vacuous
    rather than unanswered. Withholding would mean a database blip silently stops every
    homework notification in the school — a much larger risk than the one it avoids. It is
    also the ordinary state of CI, which runs the suite without a database.
    """
    from sqlalchemy.exc import OperationalError

    svc, calls = service
    monkeypatch.setattr(
        "src.curator.email_policy.curator_user_ids",
        lambda *a, **k: (_ for _ in ()).throw(OperationalError("connect", None, Exception())),
    )
    teacher = _user(db, "teacher")
    db.flush()

    svc.send_email([teacher.email], "Тема", "<p>x</p>", event_type="lesson_change")

    assert len(calls) == 1, "mail keeps flowing when the check simply cannot run"
