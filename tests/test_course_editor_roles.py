"""Course content editing (modules, lessons, steps): admins and head roles for any course, a
teacher for the course they own. Head roles were added on 2026-09-05 so they can author the SAT
checkpoint quizzes from the lesson editor."""
import pytest
from fastapi import HTTPException

from src.courses.routes.courses import _is_course_editor, update_step
from src.courses.schemas import StepCreateSchema
from src.courses.models import Step
from tests.checkpoint_fixtures import make_user, make_sat_course


@pytest.fixture
def db():
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


def test_editor_rule():
    class U:
        def __init__(self, role, id): self.role, self.id = role, id
    class C:
        teacher_id = 7
    for role in ("admin", "head_teacher", "head_curator"):
        assert _is_course_editor(U(role, 1), C())
    assert _is_course_editor(U("teacher", 7), C())
    for role in ("teacher", "curator", "student", "parent"):
        assert not _is_course_editor(U(role, 1), C())


def test_head_roles_can_save_a_step_of_a_course_they_do_not_teach(db):
    course, v, m = make_sat_course(db, n_verbal=1, n_math=1)
    owner = make_user(db, role="teacher"); course.teacher_id = owner.id; db.flush()
    step = db.query(Step).filter(Step.lesson_id == v[0].id).order_by(Step.order_index).first()
    body = StepCreateSchema(title="Quiz", content_type="quiz", content_text='{"questions": []}', order_index=0)
    for role in ("head_teacher", "head_curator"):
        out = update_step(step.id, body, current_user=make_user(db, role=role), db=db)
        assert out.title == "Quiz"
    assert update_step(step.id, body, current_user=owner, db=db).content_type == "quiz"
    for who in (make_user(db, role="teacher"), make_user(db, role="curator")):
        with pytest.raises(HTTPException) as e:
            update_step(step.id, body, current_user=who, db=db)
        assert e.value.status_code == 403
