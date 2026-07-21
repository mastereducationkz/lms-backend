import pytest
from src.schemas.models import (
    UserInDB, Group, GroupConversation, GroupConversationMember, GroupMessage,
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


def test_group_conversation_and_message_roundtrip(db):
    from src.utils.auth_utils import hash_password
    u = UserInDB(email="gc-a@test.local", name="A", role="student",
                 hashed_password=hash_password("x"), is_active=True)
    g = Group(name="GC Group", is_active=True)
    db.add_all([u, g]); db.flush()
    conv = GroupConversation(group_id=g.id, kind="class")
    db.add(conv); db.flush()
    db.add(GroupConversationMember(conversation_id=conv.id, user_id=u.id))
    db.add(GroupMessage(conversation_id=conv.id, from_user_id=u.id, content="hello"))
    db.flush()
    assert db.query(GroupMessage).filter_by(conversation_id=conv.id).count() == 1
    assert db.query(GroupConversationMember).filter_by(conversation_id=conv.id, user_id=u.id).count() == 1


def test_deleting_user_cascades_group_membership_and_messages(db):
    from src.utils.auth_utils import hash_password
    u = UserInDB(email="gc-b@test.local", name="B", role="student",
                 hashed_password=hash_password("x"), is_active=True)
    g = Group(name="GC Group2", is_active=True)
    db.add_all([u, g]); db.flush()
    conv = GroupConversation(group_id=g.id, kind="class"); db.add(conv); db.flush()
    db.add(GroupConversationMember(conversation_id=conv.id, user_id=u.id))
    db.add(GroupMessage(conversation_id=conv.id, from_user_id=u.id, content="x"))
    db.flush()
    db.delete(u); db.flush()
    assert db.query(GroupConversationMember).filter_by(user_id=u.id).count() == 0
    assert db.query(GroupMessage).filter_by(from_user_id=u.id).count() == 0
