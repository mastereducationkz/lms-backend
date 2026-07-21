import pytest
from src.schemas.models import (
    UserInDB, Group, GroupStudent, GroupConversation, GroupConversationMember,
)
from src.messages.group_membership import ensure_group_conversations, sync_group_conversation_members


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
    teacher = _u(db, "gcs-t@test.local", "teacher")
    curator = _u(db, "gcs-c@test.local", "curator")
    student = _u(db, "gcs-s@test.local", "student")
    g = Group(name="GCS", is_active=True, teacher_id=teacher.id, curator_id=curator.id)
    db.add(g); db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id))
    db.flush()
    return dict(g=g, teacher=teacher, curator=curator, student=student)


def test_new_student_added_after_provision_gets_synced_in(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    newstud = _u(db, "gc-new@test.local", "student")
    db.add(GroupStudent(group_id=s["g"].id, student_id=newstud.id)); db.flush()
    sync_group_conversation_members(db, s["g"].id); db.flush()
    class_conv = db.query(GroupConversation).filter_by(group_id=s["g"].id, kind="class").first()
    assert db.query(GroupConversationMember).filter_by(
        conversation_id=class_conv.id, user_id=newstud.id).count() == 1
