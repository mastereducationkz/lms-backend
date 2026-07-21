"""Tests for self-service account deletion (DELETE /auth/account).

Savepoint-isolated: the endpoint commits, so we roll back the outer transaction.
"""
import pytest
from fastapi import HTTPException

from src.schemas.models import UserInDB, Message
from src.utils.auth_utils import hash_password
from src.auth.routes.auth import delete_account, DeleteAccountRequest


@pytest.fixture
def db():
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    connection = engine.connect()
    trans = connection.begin()
    session = SASession(bind=connection)
    try:
        yield session
    finally:
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
