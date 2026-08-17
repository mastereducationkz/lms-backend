import json
import pytest
from datetime import datetime, timedelta, timezone

from src.schemas.models import (
    UserInDB, Group, GroupStudent, Course, Module, Lesson,
)
from src.assignments.models import (
    Assignment, AssignmentSubmission, AssignmentLinkedLesson, AssignmentDraft,
)
from src.progress.models import StudentProgress
from src.utils.auth_utils import hash_password

from src.assignments.routes.assignments import assignment_ready_for_student


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


def _student(db, email="drafts-student@test.local"):
    u = UserInDB(email=email, name="drafts-student", role="student",
                 hashed_password=hash_password("x"))
    db.add(u); db.flush()
    return u


def _group_with_student(db, student):
    g = Group(name="drafts-grp")
    db.add(g); db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id)); db.flush()
    return g


def _unit_assignment(db, group, *, linked_lessons=(), atype="multi_task"):
    """Assignment linked to `group`, of type `atype`, linked to given Lesson objects."""
    a = Assignment(title="Unit HW", assignment_type=atype,
                   content=json.dumps({"tasks": []}), max_score=10,
                   group_id=group.id, is_active=True, is_hidden=False)
    db.add(a); db.flush()
    for lesson in linked_lessons:
        db.add(AssignmentLinkedLesson(assignment_id=a.id, lesson_id=lesson.id))
    db.flush()
    return a


def _lesson(db, title="Unit A"):
    c = Course(title="C"); db.add(c); db.flush()
    m = Module(title="M", course_id=c.id); db.add(m); db.flush()
    l = Lesson(title=title, module_id=m.id); db.add(l); db.flush()
    return c, l


def _complete_lesson(db, student, course, lesson):
    db.add(StudentProgress(user_id=student.id, course_id=course.id,
                           lesson_id=lesson.id, status="completed",
                           completed_at=datetime.now(timezone.utc)))
    db.flush()


def test_ready_true_when_no_linked_lessons(db):
    student = _student(db); group = _group_with_student(db, student)
    a = _unit_assignment(db, group, linked_lessons=())
    res = assignment_ready_for_student(student.id, a, db)
    assert res["ready"] is True and res["total"] == 0


def test_not_ready_until_all_units_completed(db):
    student = _student(db); group = _group_with_student(db, student)
    c1, l1 = _lesson(db, "Unit A"); c2, l2 = _lesson(db, "Unit B")
    a = _unit_assignment(db, group, linked_lessons=(l1, l2))
    _complete_lesson(db, student, c1, l1)  # only one of two done
    res = assignment_ready_for_student(student.id, a, db)
    assert res["ready"] is False
    assert res["total"] == 2 and res["completed"] == 1
    assert res["missing"] == [{"lesson_id": l2.id, "title": "Unit B"}]


def test_ready_when_all_units_completed(db):
    student = _student(db); group = _group_with_student(db, student)
    c1, l1 = _lesson(db, "Unit A"); c2, l2 = _lesson(db, "Unit B")
    a = _unit_assignment(db, group, linked_lessons=(l1, l2))
    _complete_lesson(db, student, c1, l1); _complete_lesson(db, student, c2, l2)
    res = assignment_ready_for_student(student.id, a, db)
    assert res["ready"] is True and res["completed"] == 2 and res["missing"] == []
