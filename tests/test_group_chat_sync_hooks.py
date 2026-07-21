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


def test_sync_student_groups_returns_affected_and_channels_follow(db):
    """update_user's group diff must report which groups changed so their channels resync."""
    from src.admin.routes.admin import _sync_student_groups
    teacher = _u(db, "gcs2-t@test.local", "teacher")
    student = _u(db, "gcs2-s@test.local", "student")
    gA = Group(name="A", is_active=True, teacher_id=teacher.id)
    gB = Group(name="B", is_active=True, teacher_id=teacher.id)
    db.add_all([gA, gB]); db.flush()
    db.add(GroupStudent(group_id=gA.id, student_id=student.id)); db.flush()
    ensure_group_conversations(db, gA); ensure_group_conversations(db, gB); db.flush()

    # Move the student from A to B; the diff must report both groups as affected.
    affected = _sync_student_groups(db, student.id, [gB.id]); db.flush()
    assert affected == {gA.id, gB.id}
    for gid in affected:
        sync_group_conversation_members(db, gid)
    db.flush()

    a_class = db.query(GroupConversation).filter_by(group_id=gA.id, kind="class").first()
    b_class = db.query(GroupConversation).filter_by(group_id=gB.id, kind="class").first()
    assert db.query(GroupConversationMember).filter_by(
        conversation_id=a_class.id, user_id=student.id).count() == 0
    assert db.query(GroupConversationMember).filter_by(
        conversation_id=b_class.id, user_id=student.id).count() == 1


def test_sync_student_groups_returns_empty_when_unchanged(db):
    from src.admin.routes.admin import _sync_student_groups
    s = _setup(db)
    assert _sync_student_groups(db, s["student"].id, [s["g"].id]) == set()


def test_sync_groups_for_students_adds_then_removes_parent(db):
    """Linking a parent to a student must add them to the parents channel of that
    student's groups; unlinking must remove them (via sync_groups_for_students)."""
    from src.schemas.models import ParentStudent
    from src.messages.group_membership import sync_groups_for_students
    s = _setup(db)  # group with a teacher, curator, student
    parent = _u(db, "gcs-p@test.local", "parent")
    ensure_group_conversations(db, s["g"]); db.flush()
    parents_conv = db.query(GroupConversation).filter_by(group_id=s["g"].id, kind="parents").first()

    # Not linked yet → parent is not a member.
    assert db.query(GroupConversationMember).filter_by(
        conversation_id=parents_conv.id, user_id=parent.id).count() == 0

    # Link → sync → member.
    db.add(ParentStudent(parent_id=parent.id, student_id=s["student"].id)); db.flush()
    sync_groups_for_students(db, [s["student"].id]); db.flush()
    assert db.query(GroupConversationMember).filter_by(
        conversation_id=parents_conv.id, user_id=parent.id).count() == 1

    # Unlink → sync → removed.
    db.query(ParentStudent).filter_by(parent_id=parent.id, student_id=s["student"].id).delete(
        synchronize_session=False)
    db.flush()
    sync_groups_for_students(db, [s["student"].id]); db.flush()
    assert db.query(GroupConversationMember).filter_by(
        conversation_id=parents_conv.id, user_id=parent.id).count() == 0
