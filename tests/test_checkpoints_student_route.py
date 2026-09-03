from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition, complete_lesson_explicit,
)


@pytest.fixture
def db():
    # join_transaction_mode="create_savepoint" (SQLAlchemy 2.0) instead of the older
    # begin_nested()+after_transaction_end-listener recipe: that recipe rebuilds its savepoint by
    # reacting to the transaction the app's own db.commit() just tore down, so an app-level
    # db.rollback() straight after a commit unwinds past it and takes committed rows with it.
    # create_savepoint keeps every app-level commit/rollback nested one level down, inside a
    # connection-level transaction this fixture always rolls back. See tests/onboarding_fixtures.py.
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
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


def test_concurrent_open_race_does_not_500(db, monkeypatch):
    """Two uvicorn workers can auto-open the same checkpoint at once; uq_student_checkpoint then
    fires on the loser's INSERT. That must not surface as a 500 on GET /checkpoints/me."""
    from sqlalchemy.exc import IntegrityError
    from src.checkpoints.models import StudentCheckpoint
    from src.checkpoints.routes.checkpoints import get_my_checkpoints
    _, course, v, m, d1, d2, group, s = _world(db)
    for l in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, s, course, l)
    db.commit()

    real_flush = db.flush
    state = {"raised": False}

    def flaky_flush(*a, **kw):
        # Only trip on the INSERT that opens a checkpoint, not on incidental autoflushes.
        if not state["raised"] and any(isinstance(o, StudentCheckpoint) for o in db.new):
            state["raised"] = True
            raise IntegrityError("INSERT INTO student_checkpoints", {},
                                 Exception('duplicate key value violates unique constraint '
                                           '"uq_student_checkpoint"'))
        return real_flush(*a, **kw)

    monkeypatch.setattr(db, "flush", flaky_flush)
    out = get_my_checkpoints(current_user=s, db=db)
    assert state["raised"] is True
    assert out["enabled"] is True
    assert [i["number"] for i in out["items"]] == [1, 2]
    assert all("status" in i and "covers" in i for i in out["items"])
