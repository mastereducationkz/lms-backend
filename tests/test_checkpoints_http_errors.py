"""The app's global 403 handler rewrites every 403 body. The checkpoint gates explain *why* a
student is refused (which unit block, which checkpoint), and the web player shows
`response.data.detail` — so that reason has to survive the envelope."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lessons, make_definition,
)


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


def _client(db, user):
    from src.app import forbidden_handler
    from src.courses.routes.courses import router as courses_router
    from src.progress.routes.progress import router as progress_router
    app = FastAPI()
    app.add_exception_handler(403, forbidden_handler)
    app.include_router(courses_router, prefix="/courses")
    app.include_router(progress_router, prefix="/progress")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_dependency] = lambda: user
    return TestClient(app)


def test_checkpoint_denials_keep_their_reason_through_the_403_envelope(db):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=4, n_math=2)
    _, quiz_lessons, quiz_steps = make_quiz_lessons(db, course, 2)
    make_definition(db, course, 1, v[:2], m[0], quiz_lessons[0])
    make_definition(db, course, 2, v[2:4], m[1], quiz_lessons[1])
    group = make_group(db, enabled=True)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    db.flush()
    c = _client(db, student)

    r = c.get(f"/courses/lessons/{v[2].id}")                       # block-2 unit, checkpoint 1 not cleared
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "Forbidden" and body["status_code"] == 403   # envelope untouched
    assert body["detail"] == "Finish Checkpoint 1 before starting this unit"

    r = c.get(f"/courses/lessons/{quiz_lessons[1].id}")             # a checkpoint that is not open
    assert r.status_code == 403 and r.json()["detail"] == "This checkpoint is not open for you"

    r = c.post("/progress/quiz-attempt", json={
        "step_id": quiz_steps[0].id, "course_id": course.id, "lesson_id": quiz_lessons[0].id,
        "total_questions": 45, "is_draft": True, "answers": "{}",
    })
    assert r.status_code == 403 and r.json()["detail"].startswith("Checkpoint is locked")
