import pytest
from datetime import datetime, timedelta
from src.schemas.models import (
    UserInDB, Group, Course, CourseHeadTeacher, CourseGroupAccess,
    Event, EventGroup, Assignment,
)
from src.utils.auth_utils import hash_password
from src.assignments.routes.assignments import head_teacher_hw_gaps_today


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


def _u(db, email, role):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _lesson_today(db, group_id, creator_id):
    now = datetime.utcnow()
    ev = Event(title="Lesson", event_type="class", is_active=True,
               start_datetime=now, end_datetime=now + timedelta(hours=1), created_by=creator_id)
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group_id)); db.flush()


def test_head_teacher_hw_gaps(db):
    ht = _u(db, "htg@test.local", "head_teacher")
    course = Course(title="HTG Course", is_active=True)
    db.add(course); db.flush()
    db.add(CourseHeadTeacher(course_id=course.id, head_teacher_id=ht.id))
    gA = Group(name="HTG A", is_active=True, is_over=False)  # lesson today, no HW -> GAP
    gB = Group(name="HTG B", is_active=True, is_over=False)  # lesson today + HW today -> not gap
    gC = Group(name="HTG C", is_active=True, is_over=False)  # no lesson today -> not gap
    db.add_all([gA, gB, gC]); db.flush()
    for g in (gA, gB, gC):
        db.add(CourseGroupAccess(course_id=course.id, group_id=g.id, granted_by=ht.id, is_active=True))
    db.flush()
    _lesson_today(db, gA.id, ht.id)
    _lesson_today(db, gB.id, ht.id)
    db.add(Assignment(title="HW", assignment_type="pdf", content="{}", group_id=gB.id, is_active=True))
    db.flush()

    res = head_teacher_hw_gaps_today(current_user=ht, db=db)
    names = {x["group_name"] for x in res["groups"]}
    assert "HTG A" in names
    assert "HTG B" not in names
    assert "HTG C" not in names
    assert res["count"] == len(res["groups"])
