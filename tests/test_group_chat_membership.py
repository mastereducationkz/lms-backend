import pytest
from src.schemas.models import (
    UserInDB, Group, GroupStudent, ParentStudent, GroupConversationMember,
)
from src.messages.group_membership import (
    class_member_ids, parent_member_ids, ensure_group_conversations,
    sync_group_conversation_members,
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
    teacher = _u(db, "gc-t@test.local", "teacher")
    curator = _u(db, "gc-c@test.local", "curator")
    admin = _u(db, "gc-adm@test.local", "admin")
    student = _u(db, "gc-s@test.local", "student")
    parent = _u(db, "gc-p@test.local", "parent")
    g = Group(name="GC", is_active=True, teacher_id=teacher.id, curator_id=curator.id)
    db.add(g); db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id))
    db.add(ParentStudent(parent_id=parent.id, student_id=student.id))
    db.flush()
    return dict(g=g, teacher=teacher, curator=curator, admin=admin, student=student, parent=parent)


def test_class_members_are_students_and_staff_and_admin(db):
    s = _setup(db)
    ids = class_member_ids(db, s["g"])
    assert s["student"].id in ids and s["teacher"].id in ids
    assert s["curator"].id in ids and s["admin"].id in ids
    assert s["parent"].id not in ids


def test_parent_members_are_parents_and_staff_and_admin(db):
    s = _setup(db)
    ids = parent_member_ids(db, s["g"])
    assert s["parent"].id in ids and s["teacher"].id in ids and s["admin"].id in ids
    assert s["student"].id not in ids


def test_ensure_is_idempotent(db):
    s = _setup(db)
    ensure_group_conversations(db, s["g"]); db.flush()
    ensure_group_conversations(db, s["g"]); db.flush()
    # exactly two conversations, and the student is in exactly one (class)
    from src.schemas.models import GroupConversation
    convs = db.query(GroupConversation).filter_by(group_id=s["g"].id).all()
    assert {c.kind for c in convs} == {"class", "parents"}
    assert len(convs) == 2
    memberships = db.query(GroupConversationMember).filter_by(user_id=s["student"].id).count()
    assert memberships == 1


def test_sync_removes_departed_student(db):
    s = _setup(db)
    ensure_group_conversations(db, s["g"]); db.flush()
    db.query(GroupStudent).filter_by(group_id=s["g"].id, student_id=s["student"].id).delete()
    db.flush()
    sync_group_conversation_members(db, s["g"].id); db.flush()
    assert db.query(GroupConversationMember).filter_by(user_id=s["student"].id).count() == 0
