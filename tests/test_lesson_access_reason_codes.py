"""Every way a lesson read can be refused names itself.

A student who opens a lesson they may not open used to get a bare "Failed to load lesson data":
the client wrappers threw the axios error away, and there was nothing machine-readable to branch
on anyway. Each refusal now carries a stable `reason_code` and a Russian sentence, on both
surfaces that report one — the 403/404 envelope, and `check-access`'s 200 body.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from tests.checkpoint_fixtures import (
    complete_lesson_explicit, enroll, make_definition, make_group, make_quiz_lessons,
    make_sat_course, make_user,
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
    """The three GETs the lesson player fires, behind the app's real error envelope."""
    from src.app import forbidden_handler, not_found_handler
    from src.courses.routes.courses import router as courses_router
    from src.progress.routes.progress import router as progress_router
    app = FastAPI()
    app.add_exception_handler(403, forbidden_handler)
    app.add_exception_handler(404, not_found_handler)
    app.include_router(courses_router, prefix="/courses")
    app.include_router(progress_router, prefix="/progress")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_dependency] = lambda: user
    return TestClient(app)


def _gated_student(db):
    """A student who finished block 1, so checkpoint 1 is pending and block 2 is held back."""
    admin = make_user(db, role="admin")
    course, verbal, math = make_sat_course(db, n_verbal=4, n_math=2)
    _, quizzes, _ = make_quiz_lessons(db, course, 2)
    make_definition(db, course, 1, verbal[:2], math[0], quizzes[0])
    make_definition(db, course, 2, verbal[2:4], math[1], quizzes[1])
    group = make_group(db, enabled=True)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    db.flush()
    from src.checkpoints import service
    for lesson in (verbal[0], verbal[1], math[0]):
        complete_lesson_explicit(db, student, course, lesson)
    service.sync_student_checkpoints(db, student.id, commit=False)
    return student, course, verbal, quizzes


# ---------------------------------------------------------------- the gated cases


def test_blocked_unit_names_the_checkpoint_on_every_lesson_read(db):
    student, _, verbal, _ = _gated_student(db)
    c = _client(db, student)
    for path in (f"/courses/lessons/{verbal[2].id}", f"/courses/lessons/{verbal[2].id}/steps"):
        r = c.get(path)
        assert r.status_code == 403, path
        body = r.json()
        assert body["reason_code"] == "checkpoint_locked", path
        assert body["detail"] == "Сначала пройдите «Checkpoint 1» — после неё этот юнит откроется."
        assert body["reason_details"]["checkpoint"]["number"] == 1


def test_checkpoint_not_open_lists_the_units_still_owed(db):
    student, _, _, quizzes = _gated_student(db)
    r = _client(db, student).get(f"/courses/lessons/{quizzes[1].id}")
    assert r.status_code == 403
    body = r.json()
    assert body["reason_code"] == "checkpoint_not_open"
    # Checkpoint 2 waits on block 2, none of which is done.
    assert body["reason_details"]["missing_units"] == ["Unit 3: Verbal", "Unit 4: Verbal", "Unit 2: Math"]
    assert body["detail"].startswith("Контрольная работа «Checkpoint 2» ещё не открыта. Сначала пройдите: ")


def test_check_access_reports_the_same_reason_as_the_refusal(db):
    """The sidebar greys a lesson out from check-access; the player refuses it. Same words."""
    student, _, verbal, _ = _gated_student(db)
    c = _client(db, student)
    listed = c.get(f"/courses/lessons/{verbal[2].id}/check-access").json()
    refused = c.get(f"/courses/lessons/{verbal[2].id}").json()
    assert listed["accessible"] is False
    assert listed["reason_code"] == refused["reason_code"] == "checkpoint_locked"
    assert listed["reason"] == refused["detail"]


# ---------------------------------------------------------------- the other reasons


def test_missing_lesson_is_a_named_404_not_a_blank_one(db):
    student = make_user(db)
    r = _client(db, student).get("/courses/lessons/99000111")
    assert r.status_code == 404
    body = r.json()
    assert body["reason_code"] == "lesson_not_found"
    assert body["detail"] == "Урок не найден. Возможно, его удалили или ссылка устарела."
    assert body["error"] == "Not Found"          # envelope otherwise untouched


def test_a_routing_404_stays_generic(db):
    """Only reasons we wrote ourselves are forwarded — an unknown route says nothing."""
    student = make_user(db)
    body = _client(db, student).get("/courses/lessons/not-an-id/nope").json()
    assert "reason_code" not in body and "detail" not in body


def test_not_enrolled_says_so_on_lesson_steps_and_progress(db):
    admin = make_user(db, role="admin")
    course, verbal, _ = make_sat_course(db, n_verbal=1, n_math=1)
    outsider = make_user(db)                      # never enrolled in `course`
    db.flush()
    c = _client(db, outsider)
    for path in (f"/courses/lessons/{verbal[0].id}",
                 f"/courses/lessons/{verbal[0].id}/steps",
                 f"/progress/lesson/{verbal[0].id}/steps"):
        r = c.get(path)
        assert r.status_code == 403, path
        body = r.json()
        assert body["reason_code"] == "course_access_denied", path
        assert body["detail"] == "У вас нет доступа к этому курсу. Если это ошибка, напишите куратору."


def test_previous_lesson_reason_no_longer_leaks_module_id_and_index(db):
    admin = make_user(db, role="admin")
    course, verbal, _ = make_sat_course(db, n_verbal=3, n_math=1)
    group = make_group(db)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    db.flush()
    body = _client(db, student).get(f"/courses/lessons/{verbal[1].id}/check-access").json()
    assert body["accessible"] is False
    assert body["reason_code"] == "previous_lesson_incomplete"
    assert body["reason"] == "Сначала пройдите предыдущий урок: «Unit 1: Verbal»."
    assert "Module" not in body["reason"] and "Index" not in body["reason"]


def test_a_role_with_no_lesson_access_is_told_which_wall_it_hit(db):
    stranger = make_user(db, role="head_teacher")
    course, verbal, _ = make_sat_course(db, n_verbal=1, n_math=1)
    db.flush()
    r = _client(db, stranger).get(f"/progress/lesson/{verbal[0].id}/steps")
    assert r.status_code == 403
    assert r.json()["reason_code"] == "role_denied"


def test_every_reason_code_is_a_distinct_stable_string():
    from src.utils import lesson_access_errors as errors
    codes = [getattr(errors, n) for n in dir(errors) if n.isupper() and not n.startswith("_")]
    assert len(codes) == len(set(codes))
    assert all(c == c.lower() and " " not in c for c in codes)
