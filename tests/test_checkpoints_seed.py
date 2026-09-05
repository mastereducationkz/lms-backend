import json

import pytest

from src.schemas.models import Course, Lesson, Module, Step
from tests.checkpoint_fixtures import make_sat_course


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


def test_seed_creates_hidden_course_and_definitions_idempotently(db):
    from scripts.seed_sat_checkpoints import seed
    from src.checkpoints.models import CheckpointDefinition
    course, v, m = make_sat_course(db, n_verbal=5, n_math=2)
    unit0 = Lesson(title="Unit 0. Getting Started", module_id=v[0].module_id, order_index=0)
    db.add(unit0); db.flush()
    out = seed(db, sat_course_id=course.id, blocks=2)
    assert out["created_definitions"] == 2 and out["mapping"] == [[v[0].id, v[1].id, m[0].id], [v[2].id, v[3].id, m[1].id]]
    module = db.get(Module, out["module_id"])
    assert module.course_id == course.id and module.title == "Checkpoints"
    lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()
    assert [l.title for l in lessons] == ["Checkpoint 1", "Checkpoint 2"] and all(l.is_initially_unlocked for l in lessons)
    step = db.query(Step).filter(Step.lesson_id == lessons[0].id).one()
    assert step.content_type == "quiz" and json.loads(step.content_text) == {
        "title": "Checkpoint 1", "questions": [], "display_mode": "all_at_once",
    }
    d = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=1).one()
    assert d.quiz_lesson_id == lessons[0].id and d.is_active is False and d.total_questions == 45

    again = seed(db, sat_course_id=course.id, blocks=2)
    assert again["created_definitions"] == 0 and again["module_id"] == module.id
    assert db.query(CheckpointDefinition).filter_by(course_id=course.id).count() == 2


def test_seed_relinks_definition_whose_quiz_lesson_was_deleted(db):
    from scripts.seed_sat_checkpoints import seed
    from src.checkpoints.models import CheckpointDefinition
    course, v, m = make_sat_course(db, n_verbal=2, n_math=1)
    seed(db, sat_course_id=course.id, blocks=1)
    d = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=1).one()
    old_lesson_id = d.quiz_lesson_id
    d.quiz_lesson_id = None            # simulate the quiz lesson having been deleted
    db.query(Step).filter(Step.lesson_id == old_lesson_id).delete(synchronize_session=False)
    db.query(Lesson).filter(Lesson.id == old_lesson_id).delete(synchronize_session=False)
    db.flush()
    out = seed(db, sat_course_id=course.id, blocks=1)
    db.refresh(d)
    assert out["created_definitions"] == 0 and out["updated_definitions"] == 1
    assert d.quiz_lesson_id is not None and d.quiz_lesson_id != old_lesson_id
    assert db.get(Lesson, d.quiz_lesson_id).title == "Checkpoint 1"


def test_seed_creates_a_checkpoints_module_inside_the_sat_course(db):
    from scripts.seed_sat_checkpoints import seed
    from src.checkpoints.models import CheckpointDefinition
    course, v, m = make_sat_course(db, n_verbal=5, n_math=2)
    out = seed(db, sat_course_id=course.id, blocks=2)
    module = db.get(Module, out["module_id"])
    assert module.course_id == course.id and module.title == "Checkpoints"
    assert module.order_index == 2
    lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index).all()
    assert [l.title for l in lessons] == ["Checkpoint 1", "Checkpoint 2"]
    assert all(l.is_initially_unlocked for l in lessons)
    d = db.query(CheckpointDefinition).filter_by(course_id=course.id, number=1).one()
    assert d.quiz_lesson_id == lessons[0].id
    # the quiz now lives in the SAT course itself
    from src.checkpoints import service
    assert service.quiz_ref(db, d) == {"course_id": course.id, "lesson_id": lessons[0].id}


def test_seed_moves_legacy_lessons_out_of_the_separate_course(db):
    from scripts.seed_sat_checkpoints import seed
    from src.checkpoints.models import CheckpointDefinition
    course, v, m = make_sat_course(db, n_verbal=3, n_math=1)
    legacy = Course(title="SAT Checkpoints", course_type="sat", is_active=True)
    db.add(legacy); db.flush()
    legacy_module = Module(title="Checkpoints", course_id=legacy.id, order_index=0)
    db.add(legacy_module); db.flush()
    legacy_lesson = Lesson(title="Checkpoint 1", module_id=legacy_module.id, order_index=0,
                           is_initially_unlocked=True)
    db.add(legacy_lesson); db.flush()
    db.add(Step(lesson_id=legacy_lesson.id, title="Quiz", content_type="quiz", order_index=0,
                content_text='{"title": "Checkpoint 1", "questions": []}'))
    d = CheckpointDefinition(course_id=course.id, number=1, title="Checkpoint 1",
                             quiz_lesson_id=legacy_lesson.id, total_questions=45, is_active=False)
    db.add(d); db.flush()

    out = seed(db, sat_course_id=course.id, blocks=1)
    db.refresh(legacy_lesson)
    assert out["moved_lessons"] == 1
    assert legacy_lesson.module_id == out["module_id"]     # same lesson id, new parent
    assert d.quiz_lesson_id == legacy_lesson.id            # definition untouched
    assert db.query(Step).filter(Step.lesson_id == legacy_lesson.id).count() == 1


def test_seed_marks_quiz_lessons_as_checkpoints(db):
    from scripts.seed_sat_checkpoints import seed
    course, v, m = make_sat_course(db, n_verbal=3, n_math=1)
    out = seed(db, sat_course_id=course.id, blocks=1)
    lesson = db.query(Lesson).filter(Lesson.module_id == out["module_id"]).one()
    assert lesson.kind == "checkpoint"
    # ordinary course units are untouched
    assert db.get(Lesson, v[0].id).kind == "unit"


def test_seed_marks_moved_legacy_lessons_as_checkpoints(db):
    from scripts.seed_sat_checkpoints import seed
    from src.checkpoints.models import CheckpointDefinition
    course, v, m = make_sat_course(db, n_verbal=3, n_math=1)
    legacy_course = Course(title="SAT Checkpoints", course_type="sat", is_active=True)
    db.add(legacy_course); db.flush()
    legacy_module = Module(title="Checkpoints", course_id=legacy_course.id, order_index=0)
    db.add(legacy_module); db.flush()
    legacy_lesson = Lesson(title="Checkpoint 1", module_id=legacy_module.id, order_index=0,
                           is_initially_unlocked=True)
    db.add(legacy_lesson); db.flush()
    db.add(CheckpointDefinition(course_id=course.id, number=1, title="Checkpoint 1",
                                quiz_lesson_id=legacy_lesson.id, total_questions=45,
                                is_active=False))
    db.flush()
    seed(db, sat_course_id=course.id, blocks=1)
    db.refresh(legacy_lesson)
    assert legacy_lesson.kind == "checkpoint"
