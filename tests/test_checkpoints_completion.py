import pytest

from tests.checkpoint_fixtures import make_user, make_sat_course, complete_lesson_explicit, complete_lesson_via_steps


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


def test_explicit_and_step_based_completion_both_count(db):
    from src.checkpoints.completion import completed_lesson_ids
    course, v, m = make_sat_course(db)
    s = make_user(db)
    complete_lesson_explicit(db, s, course, v[0])
    complete_lesson_via_steps(db, s, course, m[0])
    ids = [v[0].id, v[1].id, m[0].id]
    assert completed_lesson_ids(db, s.id, ids) == {v[0].id, m[0].id}


def test_partial_steps_do_not_count(db):
    from src.checkpoints.completion import completed_lesson_ids
    from src.schemas.models import Step, StepProgress
    course, v, m = make_sat_course(db)
    s = make_user(db)
    first_step = db.query(Step).filter(Step.lesson_id == v[0].id).order_by(Step.order_index).first()
    db.add(StepProgress(user_id=s.id, course_id=course.id, lesson_id=v[0].id,
                        step_id=first_step.id, status="completed")); db.flush()
    assert completed_lesson_ids(db, s.id, [v[0].id]) == set()


def test_empty_input(db):
    from src.checkpoints.completion import completed_lesson_ids
    s = make_user(db)
    assert completed_lesson_ids(db, s.id, []) == set()
