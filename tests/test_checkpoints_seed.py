import json

import pytest

from src.schemas.models import Course, Lesson, Module, Step
from tests.checkpoint_fixtures import make_sat_course


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


def test_seed_creates_hidden_course_and_definitions_idempotently(db):
    from scripts.seed_sat_checkpoints import seed
    from src.checkpoints.models import CheckpointDefinition
    course, v, m = make_sat_course(db, n_verbal=5, n_math=2)
    unit0 = Lesson(title="Unit 0. Getting Started", module_id=v[0].module_id, order_index=0)
    db.add(unit0); db.flush()
    out = seed(db, sat_course_id=course.id, blocks=2, quiz_course_title="SAT Checkpoints (seed test)")
    assert out["created_definitions"] == 2 and out["mapping"] == [[v[0].id, v[1].id, m[0].id], [v[2].id, v[3].id, m[1].id]]
    quiz_course = db.get(Course, out["quiz_course_id"])
    assert quiz_course.title == "SAT Checkpoints (seed test)" and quiz_course.course_type == "sat"
    lessons = db.query(Lesson).join(Module).filter(Module.course_id == quiz_course.id).order_by(Lesson.order_index).all()
    assert [l.title for l in lessons] == ["Checkpoint 1", "Checkpoint 2"] and all(l.is_initially_unlocked for l in lessons)
    step = db.query(Step).filter(Step.lesson_id == lessons[0].id).one()
    assert step.content_type == "quiz" and json.loads(step.content_text) == {"title": "Checkpoint 1", "questions": []}
    d = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=1).one()
    assert d.quiz_lesson_id == lessons[0].id and d.is_active is False and d.total_questions == 45

    again = seed(db, sat_course_id=course.id, blocks=2, quiz_course_title="SAT Checkpoints (seed test)")
    assert again["created_definitions"] == 0 and again["quiz_course_id"] == quiz_course.id
    assert db.query(CheckpointDefinition).filter_by(course_id=course.id).count() == 2
