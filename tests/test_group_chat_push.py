import pytest
from src.schemas.models import UserInDB, UserPushToken
from src.utils import push_notifications


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


def test_group_push_targets_members_except_sender(db, monkeypatch):
    sender = _u(db, "gp-s@test.local", "teacher")
    m1 = _u(db, "gp-1@test.local", "student")
    m2 = _u(db, "gp-2@test.local", "student")
    for u in (m1, m2):
        db.add(UserPushToken(user_id=u.id, token=f"ExponentPushToken[{u.id}]", is_active=True, platform="ios"))
    db.flush()
    captured = {}
    class _Resp:
        status_code = 200
        def json(self): return {"data": [{"status": "ok"}, {"status": "ok"}]}
    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["messages"] = json; return _Resp()
    monkeypatch.setattr(push_notifications.requests, "post", _fake_post)

    push_notifications.send_group_message_push(
        db, member_ids=[m1.id, m2.id, sender.id], sender_name="Teacher",
        conversation_id=42, title="GC · Class", message_preview="hello", sender_id=sender.id,
    )
    tos = {m["to"] for m in captured["messages"]}
    assert tos == {f"ExponentPushToken[{m1.id}]", f"ExponentPushToken[{m2.id}]"}
    assert all(m["data"]["type"] == "group_message" and m["data"]["conversationId"] == 42
               for m in captured["messages"])
