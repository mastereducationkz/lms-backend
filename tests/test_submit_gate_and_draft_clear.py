import json
import pytest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from src.schemas.models import (
    UserInDB, Group, GroupStudent, Course, Module, Lesson,
)
from src.assignments.models import (
    Assignment, AssignmentSubmission, AssignmentLinkedLesson, AssignmentDraft,
)
from src.progress.models import StudentProgress
from src.utils.auth_utils import hash_password

from src.assignments.routes.assignments import submit_assignment, save_draft
from src.assignments.schemas import SubmitAssignmentSchema, DraftUpsertSchema


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


def test_submit_blocked_when_units_incomplete(db):
    student = _student(db); group = _group_with_student(db, student)
    c1, l1 = _lesson(db, "Unit A")
    a = _unit_assignment(db, group, linked_lessons=(l1,))  # not completed
    with pytest.raises(HTTPException) as ei:
        submit_assignment(a.id, SubmitAssignmentSchema(answers={"tasks": {}}), student, db)
    assert ei.value.status_code == 409


def test_submit_clears_draft_when_ready(db):
    student = _student(db); group = _group_with_student(db, student)
    c1, l1 = _lesson(db, "Unit A")
    a = _unit_assignment(db, group, linked_lessons=(l1,))
    _complete_lesson(db, student, c1, l1)
    save_draft(a.id, DraftUpsertSchema(answers={"tasks": {"1": "x"}}), student, db)
    submit_assignment(a.id, SubmitAssignmentSchema(answers={"tasks": {"1": "x"}}), student, db)
    assert db.query(AssignmentDraft).filter(
        AssignmentDraft.assignment_id == a.id,
        AssignmentDraft.user_id == student.id).first() is None
