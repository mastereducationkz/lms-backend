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

from src.assignments.routes.assignments import save_draft, get_draft
from src.assignments.schemas import DraftUpsertSchema


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


def test_put_draft_upserts_single_row(db):
    student = _student(db); group = _group_with_student(db, student)
    a = _unit_assignment(db, group)
    save_draft(a.id, DraftUpsertSchema(answers={"tasks": {"1": "hi"}}), student, db)
    save_draft(a.id, DraftUpsertSchema(answers={"tasks": {"1": "bye"}}), student, db)
    rows = db.query(AssignmentDraft).filter(
        AssignmentDraft.assignment_id == a.id,
        AssignmentDraft.user_id == student.id).all()
    assert len(rows) == 1
    got = get_draft(a.id, student, db)
    assert got.answers == {"tasks": {"1": "bye"}}


def test_draft_forbidden_without_active_access(db):
    import pytest
    from fastapi import HTTPException
    outsider = _student(db, email="outsider@test.local")  # not in the group
    owner = _student(db); group = _group_with_student(db, owner)
    a = _unit_assignment(db, group)
    with pytest.raises(HTTPException) as ei:
        save_draft(a.id, DraftUpsertSchema(answers={"tasks": {}}), outsider, db)
    assert ei.value.status_code == 403


def test_get_draft_404_when_assignment_missing(db):
    from fastapi import HTTPException
    student = _student(db)
    with pytest.raises(HTTPException) as ei:
        get_draft(99999999, student, db)
    assert ei.value.status_code == 404


def test_get_draft_403_when_not_visible(db):
    from fastapi import HTTPException
    outsider = _student(db, email="outsider2@test.local")  # no group/enrollment access
    course, lesson = _lesson(db)
    a = Assignment(title="Lesson HW", assignment_type="multi_task",
                    content=json.dumps({"tasks": []}), max_score=10,
                    lesson_id=lesson.id, is_active=True, is_hidden=False)
    db.add(a); db.flush()
    with pytest.raises(HTTPException) as ei:
        get_draft(a.id, outsider, db)
    assert ei.value.status_code == 403
