import pytest
from sqlalchemy.exc import IntegrityError

from tests.checkpoint_fixtures import make_user, make_group, make_sat_course, make_definition


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


def test_definition_has_ordered_required_units(db):
    course, v, m = make_sat_course(db)
    d = make_definition(db, course, 1, v[:2], m[0])
    assert [u.lesson_id for u in d.required_units] == [v[0].id, v[1].id, m[0].id]
    assert [u.kind for u in d.required_units] == ["verbal", "verbal", "math"]
    assert d.total_questions == 45


def test_definition_number_unique_per_course(db):
    course, v, m = make_sat_course(db)
    make_definition(db, course, 1, v[:2], m[0])
    with pytest.raises(IntegrityError):
        make_definition(db, course, 1, v[2:4], m[1])


def test_student_checkpoint_unique_and_defaults(db):
    from src.checkpoints.models import StudentCheckpoint, STATUS_LOCKED
    course, v, m = make_sat_course(db)
    d = make_definition(db, course, 1, v[:2], m[0])
    s = make_user(db); g = make_group(db)
    row = StudentCheckpoint(student_id=s.id, group_id=g.id, checkpoint_id=d.id,
                            checkpoint_number=1, required_unit_ids=[v[0].id, v[1].id, m[0].id])
    db.add(row); db.flush()
    assert row.status == STATUS_LOCKED and row.reopen_count == 0 and row.deadline is None
    db.add(StudentCheckpoint(student_id=s.id, group_id=g.id, checkpoint_id=d.id, checkpoint_number=1))
    with pytest.raises(IntegrityError):
        db.flush()


def test_group_checkpoint_flags_default_off(db):
    g = make_group(db)
    assert g.checkpoints_enabled is False and g.checkpoints_start_number == 1
