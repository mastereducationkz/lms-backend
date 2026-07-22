import pytest
from datetime import datetime, timedelta
from src.schemas.models import (
    UserInDB, Group, Course, CourseHeadTeacher, CourseGroupAccess,
    Event, EventGroup, Assignment,
)
from src.utils.auth_utils import hash_password
from src.assignments.routes.assignments import head_teacher_hw_gaps_by_teacher


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


def _u(db, email, role, name=None, official=None):
    u = UserInDB(email=email, name=name or email.split("@")[0], role=role,
                 official_full_name=official,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _lesson_today(db, group_id, creator_id):
    now = datetime.utcnow()
    ev = Event(title="Lesson", event_type="class", is_active=True,
               start_datetime=now, end_datetime=now + timedelta(hours=1), created_by=creator_id)
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group_id)); db.flush()


def test_hw_gaps_grouped_by_teacher_with_official_name(db):
    ht = _u(db, "htbt@test.local", "head_teacher")
    # teacher T1 has an official CRM name; T2 falls back to the LMS name.
    t1 = _u(db, "t1@test.local", "teacher", name="Abzal", official="Абзал Ермеков")
    t2 = _u(db, "t2@test.local", "teacher", name="Madina")
    course = Course(title="HTBT Course", is_active=True)
    db.add(course); db.flush()
    db.add(CourseHeadTeacher(course_id=course.id, head_teacher_id=ht.id))

    # T1: two gap groups (lesson today, no HW). T2: one gap group. gDone: lesson + HW -> not a gap.
    gA = Group(name="A", is_active=True, is_over=False, teacher_id=t1.id)
    gB = Group(name="B", is_active=True, is_over=False, teacher_id=t1.id)
    gC = Group(name="C", is_active=True, is_over=False, teacher_id=t2.id)
    gDone = Group(name="D", is_active=True, is_over=False, teacher_id=t1.id)
    db.add_all([gA, gB, gC, gDone]); db.flush()
    for g in (gA, gB, gC, gDone):
        db.add(CourseGroupAccess(course_id=course.id, group_id=g.id, granted_by=ht.id, is_active=True))
    db.flush()
    for g in (gA, gB, gC, gDone):
        _lesson_today(db, g.id, ht.id)
    db.add(Assignment(title="HW", assignment_type="pdf", content="{}", group_id=gDone.id, is_active=True))
    db.flush()

    res = head_teacher_hw_gaps_by_teacher(start_date=None, end_date=None, current_user=ht, db=db)
    by_name = {t["teacher_name"]: t for t in res["teachers"]}

    # Official ФИО is used for T1; LMS name for T2.
    assert "Абзал Ермеков" in by_name
    assert "Madina" in by_name
    assert "Abzal" not in by_name

    t1_row = by_name["Абзал Ермеков"]
    assert t1_row["groups_count"] == 2
    assert t1_row["total_lessons"] == 2
    assert {g["group_name"] for g in t1_row["groups"]} == {"A", "B"}
    # gDone (had HW) must not appear anywhere.
    all_groups = {g["group_name"] for t in res["teachers"] for g in t["groups"]}
    assert "D" not in all_groups

    assert by_name["Madina"]["groups_count"] == 1

    # A far-past date range should scope out all of today's lessons -> no gaps.
    empty = head_teacher_hw_gaps_by_teacher(
        start_date="2020-01-01", end_date="2020-01-01", current_user=ht, db=db)
    assert empty["teachers"] == []
    assert empty["start_date"] == "2020-01-01" and empty["end_date"] == "2020-01-01"
