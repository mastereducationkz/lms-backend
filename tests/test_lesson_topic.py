"""
Tests for the editable per-lesson topic feature.

Covers the set-topic endpoint (`POST /leaderboard/curator/lesson-topic`):
- teacher who owns the group can set / update / clear a topic on a class event
- topic is capped at 200 chars; blank clears it to NULL
- a non-owner teacher is rejected (403)
- an event that doesn't belong to the group is rejected (403)
- a *virtual* LessonSchedule id is materialized into a real Event and the
  topic is stored on it

Uses a real Postgres session (SessionLocal) like the other integration tests
and rolls everything back at the end, leaving no trace in the database.
The async endpoint is driven directly via asyncio.run() so no HTTP/JWT layer
is required.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.config import SessionLocal
from src.schemas.models import (
    Course,
    CourseGroupAccess,
    Event,
    EventCourse,
    EventGroup,
    Group,
    Lesson,
    LessonSchedule,
    Module,
    UserInDB,
)
from src.gamification.routes.leaderboard import (
    LessonTopicInputSchema,
    set_lesson_topic,
)


@pytest.fixture
def db():
    """
    Session bound to an outer transaction with a re-entrant SAVEPOINT, so the
    endpoint's own db.commit() only releases the savepoint — the outer
    transaction is rolled back at teardown, leaving no trace in the database.
    """
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping topic tests")

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


def _user(db, email, role):
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x", role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


def _class_event(db, group_id, created_by, title="G: Lesson 1"):
    start = datetime.now(timezone.utc).replace(microsecond=0)
    ev = Event(
        title=title,
        event_type="class",
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        created_by=created_by,
        is_active=True,
        is_recurring=False,
    )
    db.add(ev)
    db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group_id))
    db.flush()
    return ev


def _set(db, user, group_id, event_id, topic):
    return asyncio.run(
        set_lesson_topic(
            LessonTopicInputSchema(group_id=group_id, event_id=event_id, topic=topic),
            current_user=user,
            db=db,
        )
    )


def test_owner_teacher_sets_and_clears_topic(db):
    teacher = _user(db, "topic_teacher@test.local", "teacher")
    group = Group(name="Topic Group", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    ev = _class_event(db, group.id, teacher.id)

    res = _set(db, teacher, group.id, ev.id, "Present Perfect Tense")
    assert res["status"] == "success"
    assert res["event_id"] == ev.id
    assert res["topic"] == "Present Perfect Tense"
    db.refresh(ev)
    assert ev.topic == "Present Perfect Tense"

    # Blank clears to NULL
    res = _set(db, teacher, group.id, ev.id, "   ")
    assert res["topic"] is None
    db.refresh(ev)
    assert ev.topic is None


def test_topic_capped_at_200_chars(db):
    teacher = _user(db, "topic_cap@test.local", "teacher")
    group = Group(name="Cap Group", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    ev = _class_event(db, group.id, teacher.id)

    res = _set(db, teacher, group.id, ev.id, "x" * 500)
    assert len(res["topic"]) == 200


def test_non_owner_teacher_rejected(db):
    owner = _user(db, "topic_owner@test.local", "teacher")
    intruder = _user(db, "topic_intruder@test.local", "teacher")
    group = Group(name="Owned Group", teacher_id=owner.id)
    db.add(group)
    db.flush()
    ev = _class_event(db, group.id, owner.id)

    with pytest.raises(HTTPException) as exc:
        _set(db, intruder, group.id, ev.id, "Sneaky")
    assert exc.value.status_code == 403
    db.refresh(ev)
    assert ev.topic is None


def test_event_not_in_group_rejected(db):
    teacher = _user(db, "topic_admin_t@test.local", "teacher")
    group_a = Group(name="Group A", teacher_id=teacher.id)
    group_b = Group(name="Group B", teacher_id=teacher.id)
    db.add_all([group_a, group_b])
    db.flush()
    # Event belongs to group_b, but we authorize against group_a (also owned).
    ev_b = _class_event(db, group_b.id, teacher.id)

    with pytest.raises(HTTPException) as exc:
        _set(db, teacher, group_a.id, ev_b.id, "Wrong group")
    assert exc.value.status_code == 403


def test_course_linked_event_allowed(db):
    teacher = _user(db, "topic_course_t@test.local", "teacher")
    group = Group(name="Course Group", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    course = Course(title="Topic Course", is_active=True)
    db.add(course)
    db.flush()
    db.add(CourseGroupAccess(group_id=group.id, course_id=course.id, is_active=True, granted_by=teacher.id))
    db.flush()
    # Event linked to the course (not directly to the group)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    ev = Event(
        title="Course: Lesson 1", event_type="class", start_datetime=start,
        end_datetime=start + timedelta(hours=1), created_by=teacher.id, is_active=True,
    )
    db.add(ev)
    db.flush()
    db.add(EventCourse(event_id=ev.id, course_id=course.id))
    db.flush()

    res = _set(db, teacher, group.id, ev.id, "Via course")
    assert res["topic"] == "Via course"


def test_virtual_schedule_id_is_materialized(db):
    teacher = _user(db, "topic_virtual_t@test.local", "teacher")
    group = Group(name="Virtual Group", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    course = Course(title="Virtual Course", is_active=True)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="M1", order_index=0)
    db.add(module)
    db.flush()
    lesson = Lesson(module_id=module.id, title="Photosynthesis", order_index=0)
    db.add(lesson)
    db.flush()
    sched = LessonSchedule(
        group_id=group.id, lesson_id=lesson.id,
        scheduled_at=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1),
        week_number=1, is_active=True,
    )
    db.add(sched)
    db.flush()

    virtual_id = 2000000000 + sched.id
    res = _set(db, teacher, group.id, virtual_id, "Materialized topic")
    assert res["status"] == "success"
    # A real event was created (id differs from the virtual one)
    assert res["event_id"] != virtual_id
    real = db.query(Event).filter(Event.id == res["event_id"]).first()
    assert real is not None
    assert real.topic == "Materialized topic"
