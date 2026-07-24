"""requester_self_approves: a teacher who heads a subject may change their OWN lessons.

Both conditions are required — the requester must teach the group AND hold head-teacher
authority (CourseHeadTeacher, role-independent) for the group's subject.
"""
import pytest
from src.schemas.models import UserInDB, Group, Course, CourseHeadTeacher
from src.utils.auth_utils import hash_password
from src.lesson_requests.services import requester_self_approves


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


def _make(db, *, program_type, teacher_id, head_of_subject):
    course = Course(title="C", course_type="sat", is_active=True)
    db.add(course); db.flush()
    if head_of_subject is not None:
        db.add(CourseHeadTeacher(course_id=course.id, head_teacher_id=head_of_subject))
        db.flush()
    g = Group(name="G", is_active=True, is_over=False,
              teacher_id=teacher_id, program_type=program_type)
    db.add(g); db.flush()
    return g


def test_owns_and_heads_subject_self_approves(db):
    t = _u(db, "sa-owner-head@test.local", "teacher")
    g = _make(db, program_type="sat", teacher_id=t.id, head_of_subject=t.id)
    assert requester_self_approves(db, t.id, g) is True


def test_owns_but_not_head_of_subject(db):
    t = _u(db, "sa-owner-only@test.local", "teacher")
    other_head = _u(db, "sa-other-head@test.local", "head_teacher")
    g = _make(db, program_type="sat", teacher_id=t.id, head_of_subject=other_head.id)
    assert requester_self_approves(db, t.id, g) is False


def test_heads_subject_but_not_own_lesson(db):
    t = _u(db, "sa-head-notowner@test.local", "teacher")
    other_teacher = _u(db, "sa-otherteacher@test.local", "teacher")
    g = _make(db, program_type="sat", teacher_id=other_teacher.id, head_of_subject=t.id)
    assert requester_self_approves(db, t.id, g) is False
