"""
AssignmentDraft is an isolated server-side autosave of a student's
in-progress answers for an assignment — never counted as a submission.
This test just pins the model shape: one draft row per (assignment, user).
"""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

# Must be imported before any direct `src.<domain>.models` submodule import below —
# entering via the aggregator package first avoids a self-referential circular
# import (src.assignments.models -> src.models -> src.assignments.models).
import src.models  # noqa: F401

from src.assignments.models import AssignmentDraft, Assignment
from src.courses.models import Group, GroupStudent
from src.auth.models import UserInDB


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping assignment draft tests")

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


def _student(db):
    student = UserInDB(
        email="draft_student@test.local", name="Draft Student",
        hashed_password="x", role="student", is_active=True,
    )
    db.add(student)
    db.flush()
    return student


def _group_with_student(db, student):
    teacher = UserInDB(
        email="draft_teacher@test.local", name="Draft Teacher",
        hashed_password="x", role="teacher", is_active=True,
    )
    db.add(teacher)
    db.flush()
    group = Group(name="Draft Group", teacher_id=teacher.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()
    return group


def _unit_assignment(db, group):
    assignment = Assignment(
        group_id=group.id, title="Unit Assignment",
        assignment_type="quiz", content=json.dumps({"tasks": []}),
    )
    db.add(assignment)
    db.flush()
    return assignment


def test_draft_unique_per_assignment_user(db):
    student = _student(db)
    group = _group_with_student(db, student)
    a = _unit_assignment(db, group)
    db.add(AssignmentDraft(assignment_id=a.id, user_id=student.id,
                           answers=json.dumps({"tasks": {}}),
                           updated_at=datetime.now(timezone.utc)))
    db.flush()
    db.add(AssignmentDraft(assignment_id=a.id, user_id=student.id,
                           answers=json.dumps({"tasks": {"1": "y"}}),
                           updated_at=datetime.now(timezone.utc)))
    with pytest.raises(IntegrityError):
        db.flush()
