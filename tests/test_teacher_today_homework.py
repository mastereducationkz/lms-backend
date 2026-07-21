import pytest
from src.schemas.models import UserInDB, Group, Assignment
from src.utils.auth_utils import hash_password
from src.assignments.routes.assignments import teacher_today_homework


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
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def test_today_homework_coverage(db):
    teacher = _u(db, "tth-t@test.local", "teacher")
    g1 = Group(name="TTH A", is_active=True, teacher_id=teacher.id)
    g2 = Group(name="TTH B", is_active=True, teacher_id=teacher.id)
    db.add_all([g1, g2]); db.flush()
    db.add(Assignment(title="HW1", assignment_type="pdf", content="{}",
                      group_id=g1.id, is_active=True))
    db.flush()

    res = teacher_today_homework(current_user=teacher, db=db)
    assert res["total_groups"] == 2
    assert res["assigned_count"] == 1
    assert res["missing_count"] == 1
    gmap = {g["group_name"]: g for g in res["groups"]}
    assert gmap["TTH A"]["has_homework_today"] is True
    assert gmap["TTH A"]["assignments"][0]["title"] == "HW1"
    assert gmap["TTH B"]["has_homework_today"] is False
