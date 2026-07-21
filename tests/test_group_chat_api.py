import pytest
from fastapi import HTTPException
from src.schemas.models import UserInDB, Group, GroupStudent, ParentStudent, GroupConversation
from src.messages.group_membership import ensure_group_conversations
from src.messages.routes.group_messages import (
    list_group_conversations, get_group_messages, post_group_message, PostGroupMessage,
)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
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
        session.close()
        trans.rollback()
        connection.close()


def _u(db, email, role):
    from src.utils.auth_utils import hash_password
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _setup(db):
    teacher = _u(db, "gca-t@test.local", "teacher")
    curator = _u(db, "gca-c@test.local", "curator")
    admin = _u(db, "gca-adm@test.local", "admin")
    student = _u(db, "gca-s@test.local", "student")
    parent = _u(db, "gca-p@test.local", "parent")
    g = Group(name="GCA", is_active=True, teacher_id=teacher.id, curator_id=curator.id)
    db.add(g); db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id))
    db.add(ParentStudent(parent_id=parent.id, student_id=student.id))
    db.flush()
    return dict(g=g, teacher=teacher, curator=curator, admin=admin, student=student, parent=parent)


def test_post_and_list_via_router(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = db.query(GroupConversation).filter_by(group_id=s["g"].id, kind="class").first()
    post_group_message(conv.id, PostGroupMessage(content="hello"), current_user=s["student"], db=db)
    convs = list_group_conversations(current_user=s["student"], db=db)
    assert any(c["id"] == conv.id and c["unread_count"] == 0 for c in convs)


def test_non_member_get_is_403(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = db.query(GroupConversation).filter_by(group_id=s["g"].id, kind="parents").first()
    with pytest.raises(HTTPException) as exc:
        get_group_messages(conv.id, current_user=s["student"], db=db)
    assert exc.value.status_code == 403
