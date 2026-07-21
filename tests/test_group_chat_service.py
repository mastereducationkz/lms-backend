import pytest
from src.schemas.models import UserInDB, Group, GroupStudent, ParentStudent, GroupConversation
from src.messages.group_membership import ensure_group_conversations
from src.messages import group_service


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
    admin = _u(db, "gcs-adm@test.local", "admin")
    student = _u(db, "gcs-s@test.local", "student")
    parent = _u(db, "gcs-p@test.local", "parent")
    g = Group(name="GCS", is_active=True, teacher_id=teacher.id, curator_id=curator.id)
    db.add(g); db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id))
    db.add(ParentStudent(parent_id=parent.id, student_id=student.id))
    db.flush()
    return dict(g=g, teacher=teacher, curator=curator, admin=admin, student=student, parent=parent)


def _conv(db, group_id, kind):
    return db.query(GroupConversation).filter_by(group_id=group_id, kind=kind).first()


def test_member_can_post_and_read(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "class")
    msg = group_service.post_message(db, s["student"].id, conv.id, "hi"); db.flush()
    assert msg["content"] == "hi" and msg["from_user_id"] == s["student"].id
    msgs = group_service.get_messages(db, s["teacher"].id, conv.id)
    assert any(m["id"] == msg["id"] for m in msgs)


def test_non_member_cannot_read(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "parents")  # student is NOT in parents channel
    with pytest.raises(PermissionError):
        group_service.get_messages(db, s["student"].id, conv.id)


def test_student_in_special_group_cannot_post(db):
    s = _setup(db)
    s["g"].is_special = True; db.flush()
    ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "class")
    with pytest.raises(ValueError):
        group_service.post_message(db, s["student"].id, conv.id, "hi")
    # staff can still post
    assert group_service.post_message(db, s["teacher"].id, conv.id, "ok")["content"] == "ok"


def test_unread_count_uses_last_read(db):
    s = _setup(db); ensure_group_conversations(db, s["g"]); db.flush()
    conv = _conv(db, s["g"].id, "class")
    group_service.post_message(db, s["teacher"].id, conv.id, "m1"); db.flush()
    convs = {c["id"]: c for c in group_service.list_conversations(db, s["student"].id)}
    assert convs[conv.id]["unread_count"] == 1
    group_service.mark_read(db, s["student"].id, conv.id); db.flush()
    convs = {c["id"]: c for c in group_service.list_conversations(db, s["student"].id)}
    assert convs[conv.id]["unread_count"] == 0
