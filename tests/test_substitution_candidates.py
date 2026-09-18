"""Who may be offered to cover a lesson.

The list was built from the teachers who **own** a group on the course, so anyone who teaches
without owning one was invisible: on production Қайратқызы Дина had taught 57 NUET lessons in a
month — every one of them somebody else's group — and could never be offered a NUET substitution
(reported 2026-09-18). Three more active teachers were in the same position.

That contradicts the invariant this codebase already keeps (see test_substitution_ownership.py):
`groups.teacher_id` is ownership, `events.teacher_id` is who taught it. Duty, pay — and now the
candidate list — follow the lesson.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.lesson_requests.routes import router as lesson_requests_router
from src.routes.auth import get_current_user_dependency
from src.schemas.models import (Course, CourseGroupAccess, Event, EventGroup, Group, GroupStudent,
                                UserInDB)

WHEN = datetime(2026, 9, 24, 13, 0)      # 18:00 Almaty, the slot to cover


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
        session.close()
        trans.rollback()
        connection.close()


def _user(db, name, role="teacher", **extra):
    from src.utils.auth_utils import hash_password
    user = UserInDB(email=f"sub-{datetime.now().timestamp():.6f}@test.local", name=name, role=role,
                    hashed_password=hash_password("x"), is_active=True, no_substitutions=False, **extra)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def world(db):
    """A NUET course with one group, its owner, and a teacher who only ever covers lessons."""
    owner = _user(db, "Орынбасар Ақжол")
    course = Course(title="NUET", description="", teacher_id=owner.id)
    db.add(course)
    db.flush()

    group = Group(name="NUET August 1 2026 - Ақжол", teacher_id=owner.id, is_active=True,
                  is_over=False, program_type="nuet")
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=_user(db, "Ученик", "student").id))
    db.add(CourseGroupAccess(course_id=course.id, group_id=group.id, granted_by=owner.id, is_active=True))
    db.flush()

    def lesson(*, teacher, when=WHEN - timedelta(days=7), minutes=60):
        event = Event(title="NUET, урок", event_type="class", start_datetime=when,
                      end_datetime=when + timedelta(minutes=minutes), created_by=owner.id,
                      teacher_id=teacher.id, is_active=True)
        db.add(event)
        db.flush()
        db.add(EventGroup(event_id=event.id, group_id=group.id))
        db.flush()
        return event

    substitute = _user(db, "Қайратқызы Дина")      # owns no group anywhere
    lesson(teacher=substitute)                      # but has taught this group
    stranger = _user(db, "Преподаватель другого курса")

    return {"owner": owner, "course": course, "group": group, "substitute": substitute,
            "stranger": stranger, "lesson": lesson}


@pytest.fixture
def client(db):
    def _for(user):
        app = FastAPI()
        app.include_router(lesson_requests_router, prefix="/lesson-requests")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_dependency] = lambda: user
        return TestClient(app)
    return _for


def _offered(client, world, user=None):
    response = client(user or world["owner"]).get(
        "/lesson-requests/teachers/available",
        params={"datetime_str": WHEN.isoformat(), "group_id": world["group"].id})
    assert response.status_code == 200, response.text
    return {t["name"] for t in response.json()["available_teachers"]}


def test_someone_who_has_taught_the_course_can_cover_it(client, world):
    """The reported case: 57 NUET lessons taught, never offered, because she owns no group."""
    assert "Қайратқызы Дина" in _offered(client, world)


def test_someone_who_never_taught_the_course_is_not_offered(client, world):
    assert "Преподаватель другого курса" not in _offered(client, world)


def test_the_teacher_asking_is_not_offered_to_themselves(client, world):
    assert "Орынбасар Ақжол" not in _offered(client, world)


def test_opting_out_of_substitutions_still_keeps_a_teacher_off_the_list(client, world, db):
    world["substitute"].no_substitutions = True
    db.flush()
    assert "Қайратқызы Дина" not in _offered(client, world)


def test_a_teacher_who_has_left_is_not_offered(client, world, db):
    world["substitute"].is_active = False
    db.flush()
    assert "Қайратқызы Дина" not in _offered(client, world)


def test_a_teacher_already_teaching_at_that_hour_is_not_offered(client, world):
    world["lesson"](teacher=world["substitute"], when=WHEN)   # busy exactly then
    assert "Қайратқызы Дина" not in _offered(client, world)


def test_a_lesson_long_ago_does_not_make_somebody_a_candidate_for_ever(client, world, db):
    """Covering one lesson last year is not a standing qualification to teach the course."""
    forgotten = _user(db, "Преподаватель из прошлого сезона")
    world["lesson"](teacher=forgotten, when=WHEN - timedelta(days=400))
    assert "Преподаватель из прошлого сезона" not in _offered(client, world)
