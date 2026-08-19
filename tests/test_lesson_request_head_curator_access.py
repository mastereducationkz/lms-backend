"""Head curators may resolve any lesson request (full powers, unscoped)."""
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession

from src.schemas.models import UserInDB
from src.utils.auth_utils import hash_password
from src.lesson_requests.services import user_can_resolve_request


@pytest.fixture
def db():
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
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
        session.close(); trans.rollback(); connection.close()


def _u(db, email, role):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def test_head_curator_can_resolve_any_group(db):
    hc = _u(db, "hc-resolve@test.local", "head_curator")
    # group_id 999999 need not exist — head_curator is unscoped
    assert user_can_resolve_request(db, hc, 999999) is True


def test_plain_curator_cannot_resolve(db):
    c = _u(db, "c-resolve@test.local", "curator")
    assert user_can_resolve_request(db, c, 999999) is False
