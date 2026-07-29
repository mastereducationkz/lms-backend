"""Tests for the /favorite-steps router (real Postgres SAVEPOINT; endpoints called directly)."""
import pytest
from fastapi import HTTPException

from src.schemas.models import Course, Module, Lesson, Step, UserInDB
from src.content.schemas import FavoriteStepCreateSchema
from src.content.routes.favorite_steps import (
    add_favorite_step, get_favorite_steps, remove_favorite_step, check_step_is_favorite, router,
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
        pytest.skip("No database available (requires Postgres); skipping")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        trans.rollback()
        connection.close()


def _student(db, email):
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x", role="student", is_active=True)
    db.add(u); db.flush()
    return u


def _make_step(db, title="Step A", content_type="text", order_index=3):
    course = Course(title="C1", is_active=True); db.add(course); db.flush()
    module = Module(course_id=course.id, title="M1", order_index=0); db.add(module); db.flush()
    lesson = Lesson(module_id=module.id, title="L1", order_index=0); db.add(lesson); db.flush()
    step = Step(lesson_id=lesson.id, title=title, content_type=content_type, order_index=order_index)
    db.add(step); db.flush()
    return course, lesson, step


def test_add_creates_and_resolves(db):
    student = _student(db, "fs_a@test.local")
    course, lesson, step = _make_step(db, title="Intro", content_type="quiz", order_index=5)
    res = add_favorite_step(FavoriteStepCreateSchema(step_id=step.id), current_user=student, db=db)
    assert res.step_id == step.id
    assert res.lesson_id == lesson.id
    assert res.course_id == course.id
    assert res.order_index == 5
    assert res.step_title == "Intro"
    assert res.content_type == "quiz"
    assert res.course_title == "C1"
    assert res.lesson_title == "L1"


def test_add_is_idempotent(db):
    from src.schemas.models import FavoriteStep
    student = _student(db, "fs_b@test.local")
    _, _, step = _make_step(db)
    add_favorite_step(FavoriteStepCreateSchema(step_id=step.id), current_user=student, db=db)
    add_favorite_step(FavoriteStepCreateSchema(step_id=step.id), current_user=student, db=db)
    count = db.query(FavoriteStep).filter(
        FavoriteStep.user_id == student.id, FavoriteStep.step_id == step.id
    ).count()
    assert count == 1


def test_add_bad_step_404(db):
    student = _student(db, "fs_c@test.local")
    with pytest.raises(HTTPException) as ei:
        add_favorite_step(FavoriteStepCreateSchema(step_id=999999), current_user=student, db=db)
    assert ei.value.status_code == 404


def test_get_is_enriched_and_scoped(db):
    a = _student(db, "fs_d@test.local")
    b = _student(db, "fs_e@test.local")
    _, _, step = _make_step(db, title="Only A")
    add_favorite_step(FavoriteStepCreateSchema(step_id=step.id), current_user=a, db=db)
    assert get_favorite_steps(current_user=b, db=db) == []
    rows = get_favorite_steps(current_user=a, db=db)
    assert len(rows) == 1
    assert rows[0].step_title == "Only A"


def test_check_reflects_state(db):
    student = _student(db, "fs_f@test.local")
    _, _, step = _make_step(db)
    assert check_step_is_favorite(step.id, current_user=student, db=db)["is_favorite"] is False
    add_favorite_step(FavoriteStepCreateSchema(step_id=step.id), current_user=student, db=db)
    assert check_step_is_favorite(step.id, current_user=student, db=db)["is_favorite"] is True


def test_delete_removes_and_404_when_absent(db):
    student = _student(db, "fs_g@test.local")
    _, _, step = _make_step(db)
    add_favorite_step(FavoriteStepCreateSchema(step_id=step.id), current_user=student, db=db)
    remove_favorite_step(step.id, current_user=student, db=db)
    assert check_step_is_favorite(step.id, current_user=student, db=db)["is_favorite"] is False
    with pytest.raises(HTTPException) as ei:
        remove_favorite_step(step.id, current_user=student, db=db)
    assert ei.value.status_code == 404


def test_router_paths_registered():
    # No DB needed: verify the four endpoints exist with expected methods/paths.
    registered = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (("POST",), "/") in registered or (("POST",), "") in registered
    assert (("GET",), "/") in registered or (("GET",), "") in registered
    assert (("DELETE",), "/{step_id}") in registered
    assert (("GET",), "/check/{step_id}") in registered
