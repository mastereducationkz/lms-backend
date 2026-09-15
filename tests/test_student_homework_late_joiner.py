"""A student who joins a group late must not inherit the group's old homework as overdue.

Case (2026-09-15): a student added to «July 9 SAT - Даниил» on 21.08 saw 21 homework items
from 10.07–21.08 as «Просрочено». The child was distressed and the parent kept calling the
curator. Homework whose deadline fell on or before the day the student joined the group was
never theirs to do, so it is left out of the student's lists — unless they submitted it anyway,
or a teacher gave them an extension on it (then it is theirs again).
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession

from src.config import engine
from src.schemas.models import UserInDB, Group, GroupStudent
from src.assignments.models import Assignment, AssignmentExtension, AssignmentSubmission
from src.utils.auth_utils import hash_password
from src.assignments.routes.assignments import _student_assignments, get_assignments


JOINED = datetime(2026, 8, 21)


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


def _user(db, email, role="student"):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"))
    db.add(u)
    db.flush()
    return u


def _group_with(db, student, joined_at, name="July 9 SAT"):
    g = Group(name=name)
    db.add(g)
    db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id, created_at=joined_at))
    db.flush()
    return g


def _homework(db, group, due, title="HW"):
    a = Assignment(title=title, assignment_type="multi_task",
                   content=json.dumps({"tasks": []}), max_score=10,
                   group_id=group.id, is_active=True, is_hidden=False, due_date=due)
    db.add(a)
    db.flush()
    return a


def _listed(db, student):
    return {a.id for a in get_assignments(current_user=student, db=db, skip=0, limit=100)}


def test_homework_due_before_joining_is_not_listed(db):
    student = _user(db, "late-joiner@test.local")
    group = _group_with(db, student, JOINED)
    july = _homework(db, group, datetime(2026, 7, 10, 14, 0), "Bluebook Practice 5")
    august = _homework(db, group, datetime(2026, 8, 19, 14, 0), "HW verbal unit 14")

    listed = _listed(db, student)

    assert july.id not in listed
    assert august.id not in listed


def test_homework_due_on_the_join_day_is_not_listed(db):
    # Membership is stamped with a date (00:00); a deadline later that same day was set
    # before the student could have seen it.
    student = _user(db, "join-day@test.local")
    group = _group_with(db, student, JOINED)
    same_day = _homework(db, group, datetime(2026, 8, 21, 14, 0), "Math unit 8")

    assert same_day.id not in _listed(db, student)


def test_homework_due_after_joining_is_listed(db):
    student = _user(db, "after-join@test.local")
    group = _group_with(db, student, JOINED)
    next_day = _homework(db, group, datetime(2026, 8, 22, 14, 0))
    undated = _homework(db, group, None, "No deadline")

    listed = _listed(db, student)

    assert next_day.id in listed
    assert undated.id in listed


def test_submitted_pre_join_homework_stays_listed(db):
    student = _user(db, "did-it-anyway@test.local")
    group = _group_with(db, student, JOINED)
    old = _homework(db, group, datetime(2026, 7, 10, 14, 0))
    db.add(AssignmentSubmission(assignment_id=old.id, user_id=student.id, answers="{}",
                                max_score=10, score=8, is_graded=True, is_hidden=False))
    db.flush()

    assert old.id in _listed(db, student)


def test_pre_join_homework_with_an_extension_stays_listed(db):
    student = _user(db, "extended@test.local")
    teacher = _user(db, "teacher-ext@test.local", role="teacher")
    group = _group_with(db, student, JOINED)
    old = _homework(db, group, datetime(2026, 7, 10, 14, 0))
    db.add(AssignmentExtension(assignment_id=old.id, student_id=student.id,
                               extended_deadline=datetime(2026, 9, 30), granted_by=teacher.id))
    db.flush()

    assert old.id in _listed(db, student)


def test_rule_is_per_student(db):
    early = _user(db, "early@test.local")
    late = _user(db, "late@test.local")
    group = _group_with(db, early, datetime(2026, 7, 1))
    db.add(GroupStudent(group_id=group.id, student_id=late.id, created_at=JOINED))
    db.flush()
    hw = _homework(db, group, datetime(2026, 7, 10, 14, 0))

    assert hw.id in _listed(db, early)
    assert hw.id not in _listed(db, late)


def test_student_assignments_feed_applies_the_same_rule(db):
    # Parent portal, reports, the updates widget and progress all read _student_assignments.
    student = _user(db, "feed@test.local")
    group = _group_with(db, student, JOINED)
    old = _homework(db, group, datetime(2026, 7, 10, 14, 0))
    new = _homework(db, group, datetime(2026, 9, 1, 14, 0))

    ids = {a.id for a in _student_assignments(db, student.id).all()}

    assert old.id not in ids
    assert new.id in ids
