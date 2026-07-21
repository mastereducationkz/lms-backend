"""Tests for self-service account deletion (DELETE /auth/account).

Savepoint-isolated: the endpoint commits, so we roll back the outer transaction.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.schemas.models import UserInDB, Message, Event, EventParticipant
from src.utils.auth_utils import hash_password
from src.auth.routes.auth import delete_account, DeleteAccountRequest


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping account-deletion tests")

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


def _make_user(db, *, role: str, email: str) -> UserInDB:
    u = UserInDB(email=email, name="Test", role=role,
                 hashed_password=hash_password("secret123"), is_active=True)
    db.add(u)
    db.flush()
    return u


def test_student_can_delete_own_account(db):
    student = _make_user(db, role="student", email="del-student@test.local")
    sid = student.id
    result = delete_account(DeleteAccountRequest(current_password="secret123"),
                            current_user=student, db=db)
    assert db.query(UserInDB).filter(UserInDB.id == sid).first() is None


def test_wrong_password_rejected(db):
    student = _make_user(db, role="student", email="del-wrong@test.local")
    with pytest.raises(HTTPException) as exc:
        delete_account(DeleteAccountRequest(current_password="WRONG"),
                       current_user=student, db=db)
    assert exc.value.status_code == 400
    assert db.query(UserInDB).filter(UserInDB.id == student.id).first() is not None


def test_staff_cannot_self_delete(db):
    teacher = _make_user(db, role="teacher", email="del-teacher@test.local")
    with pytest.raises(HTTPException) as exc:
        delete_account(DeleteAccountRequest(current_password="secret123"),
                       current_user=teacher, db=db)
    assert exc.value.status_code == 403
    assert db.query(UserInDB).filter(UserInDB.id == teacher.id).first() is not None


def test_student_with_messages_deletes_clean(db):
    a = _make_user(db, role="student", email="del-a@test.local")
    b = _make_user(db, role="student", email="del-b@test.local")
    db.add(Message(from_user_id=a.id, to_user_id=b.id, content="hi"))
    db.flush()
    delete_account(DeleteAccountRequest(current_password="secret123"),
                   current_user=a, db=db)
    assert db.query(Message).filter(Message.from_user_id == a.id).count() == 0


def test_student_with_event_registration_deletes_clean(db):
    admin = _make_user(db, role="admin", email="del-event-admin@test.local")
    student = _make_user(db, role="student", email="del-event-student@test.local")
    sid = student.id
    now = datetime.utcnow()
    ev = Event(
        title="Test Event",
        event_type="webinar",
        start_datetime=now,
        end_datetime=now + timedelta(hours=1),
        created_by=admin.id,
    )
    db.add(ev)
    db.flush()
    db.add(EventParticipant(event_id=ev.id, user_id=sid))
    db.flush()

    delete_account(DeleteAccountRequest(current_password="secret123"),
                   current_user=student, db=db)

    assert db.query(UserInDB).filter(UserInDB.id == sid).first() is None
    assert db.query(EventParticipant).filter(EventParticipant.user_id == sid).count() == 0
