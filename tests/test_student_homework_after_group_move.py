"""Regression: a student who is moved out of the group where their graded
homework lives must still see that homework on GET /assignments/.

Bug: the student branch of get_assignments scoped the list strictly to the
student's *current* group memberships (or active enrolled-course lessons), so a
student transferred into a different group lost all access to their submitted/
graded homework — the page went empty while the unseen-graded badge kept
counting those submissions. See assignment_visible_to_student(), which already
whitelists submitted assignments; the base query just didn't include them.
"""
import json

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession

from src.config import engine
from src.schemas.models import UserInDB, Group, GroupStudent
from src.assignments.models import Assignment, AssignmentSubmission
from src.utils.auth_utils import hash_password
from src.assignments.routes.assignments import get_assignments


@pytest.fixture
def db():
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
        session.close()
        trans.rollback()
        connection.close()


def _student(db, email="moved-student@test.local"):
    u = UserInDB(email=email, name="moved-student", role="student",
                 hashed_password=hash_password("x"))
    db.add(u)
    db.flush()
    return u


def _group(db, name):
    g = Group(name=name)
    db.add(g)
    db.flush()
    return g


def _group_assignment(db, group):
    a = Assignment(title="SAT HW", assignment_type="multi_task",
                   content=json.dumps({"tasks": []}), max_score=10,
                   group_id=group.id, is_active=True, is_hidden=False)
    db.add(a)
    db.flush()
    return a


def _graded_submission(db, student, assignment):
    s = AssignmentSubmission(assignment_id=assignment.id, user_id=student.id,
                             answers="{}", max_score=10, score=8,
                             is_graded=True, is_hidden=False, seen_by_student=False)
    db.add(s)
    db.flush()
    return s


def test_student_sees_graded_homework_from_former_group(db):
    student = _student(db)

    # Former group where the graded homework lives — student is NO LONGER a member.
    former = _group(db, "June 31 SAT")
    assignment = _group_assignment(db, former)
    _graded_submission(db, student, assignment)

    # Current group the student was moved into — has no assignments.
    current = _group(db, "Bonus Group SAT")
    db.add(GroupStudent(group_id=current.id, student_id=student.id))
    db.flush()

    result = get_assignments(current_user=student, db=db, skip=0, limit=100)
    ids = {a.id for a in result}

    # Before the fix this was empty (assignment.group_id not in current memberships,
    # assignment has no lesson_id) — the student's own graded homework vanished.
    assert assignment.id in ids


def test_student_does_not_see_unrelated_group_homework(db):
    """The submitted-assignments branch must not leak other groups' homework."""
    student = _student(db, email="moved-student-2@test.local")
    current = _group(db, "Bonus Group SAT 2")
    db.add(GroupStudent(group_id=current.id, student_id=student.id))
    db.flush()

    # An assignment in a group the student never touched, with no submission by them.
    other = _group(db, "Some Other Group")
    _group_assignment(db, other)

    result = get_assignments(current_user=student, db=db, skip=0, limit=100)
    assert result == []
