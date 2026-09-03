from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition, complete_lesson_explicit,
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
        session.close(); trans.rollback(); connection.close()


def _world(db, enabled=True):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=4, n_math=2)
    _, quiz_lesson, _ = make_quiz_lesson(db)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lesson)
    d2 = make_definition(db, course, 2, v[2:4], m[1], quiz_lesson)
    group = make_group(db, enabled=enabled)
    s = make_user(db)
    enroll(db, s, group, course, admin)
    return admin, course, v, m, d1, d2, group, s


def test_disabled_group_sees_nothing(db):
    from src.checkpoints.routes.checkpoints import get_my_checkpoints
    _, _, _, _, _, _, _, s = _world(db, enabled=False)
    out = get_my_checkpoints(current_user=s, db=db)
    assert out == {"enabled": False, "items": []}


def test_enabled_group_lists_all_with_status_and_auto_opens(db):
    from src.checkpoints.routes.checkpoints import get_my_checkpoints
    _, course, v, m, d1, d2, group, s = _world(db)
    for l in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, s, course, l)
    out = get_my_checkpoints(current_user=s, db=db)
    assert out["enabled"] is True
    assert [(i["number"], i["status"]) for i in out["items"]] == [(1, "available"), (2, "locked")]
    assert out["items"][0]["deadline"] is not None and out["items"][0]["quiz"] is not None
    assert out["items"][1]["locked_reason"].startswith("Locked — waiting for")


def test_staff_forbidden(db):
    from src.checkpoints.routes.checkpoints import get_my_checkpoints
    t = make_user(db, role="teacher")
    with pytest.raises(HTTPException) as e:
        get_my_checkpoints(current_user=t, db=db)
    assert e.value.status_code == 403
