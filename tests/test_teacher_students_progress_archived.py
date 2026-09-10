"""Teacher dashboard students-progress: archived groups and deactivated students.

39 of 60 teachers on production have archived groups and the dashboard showed none of
them — a finished cohort vanished from the teacher who taught it. The endpoint now has
the same two switches as the curator journal: ``include_archived`` and
``include_inactive``. Defaults stay "active students of active groups".
"""
import pytest

from src.schemas.models import (  # noqa: F401  (import-order guard: shim first)
    Course,
    CourseGroupAccess,
    Group,
    GroupStudent,
    UserInDB,
)
from src.admin.routes.dashboard import get_teacher_students_progress

_endpoint = getattr(get_teacher_students_progress, "__wrapped__", get_teacher_students_progress)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
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


def _user(db, email, role="student", active=True):
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x",
                 role=role, is_active=active)
    db.add(u)
    db.flush()
    return u


def _group(db, name, teacher, active=True):
    g = Group(name=name, teacher_id=teacher.id, is_active=active)
    db.add(g)
    db.flush()
    return g


@pytest.fixture
def seeded(db):
    teacher = _user(db, "tsp.teacher@test.local", role="teacher")
    other_teacher = _user(db, "tsp.other@test.local", role="teacher")

    current = _group(db, "TSP Current", teacher)
    archived = _group(db, "TSP Archived", teacher, active=False)
    foreign = _group(db, "TSP Foreign", other_teacher)

    course = Course(title="TSP Course", description="", is_active=True)
    db.add(course)
    db.flush()
    for g in (current, archived, foreign):
        db.add(CourseGroupAccess(group_id=g.id, course_id=course.id, is_active=True,
                                 granted_by=teacher.id))

    s_current = _user(db, "tsp.current@test.local")
    s_archived = _user(db, "tsp.archived@test.local")
    s_both = _user(db, "tsp.both@test.local")
    s_inactive = _user(db, "tsp.inactive@test.local", active=False)
    s_foreign = _user(db, "tsp.foreign@test.local")

    # s_both joins the ARCHIVED group first, so id order alone would pick it.
    for student, group in ((s_both, archived), (s_current, current), (s_archived, archived),
                           (s_both, current), (s_inactive, current), (s_foreign, foreign)):
        db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()
    return {
        "teacher": teacher, "current": current, "archived": archived,
        "s_current": s_current, "s_archived": s_archived, "s_both": s_both,
        "s_inactive": s_inactive, "s_foreign": s_foreign,
    }


def _rows(db, teacher, *, include_archived=False, include_inactive=False):
    res = _endpoint(include_archived=include_archived, include_inactive=include_inactive,
                    current_user=teacher, db=db)
    return {r["student_id"]: r for r in res["students_progress"]}


def test_default_shows_active_students_of_active_groups(db, seeded):
    rows = _rows(db, seeded["teacher"])
    assert set(rows) == {seeded["s_current"].id, seeded["s_both"].id}
    assert not any(r["group_is_archived"] or r["is_inactive"] for r in rows.values())


def test_include_archived_brings_back_finished_cohorts(db, seeded):
    rows = _rows(db, seeded["teacher"], include_archived=True)
    assert seeded["s_archived"].id in rows
    arch_row = rows[seeded["s_archived"].id]
    assert arch_row["group_is_archived"] is True
    assert arch_row["group_id"] == seeded["archived"].id

    # A student in both a current and an archived group is listed under the
    # current one, even though the archived membership is older.
    both = rows[seeded["s_both"].id]
    assert both["group_id"] == seeded["current"].id
    assert both["group_is_archived"] is False


def test_include_inactive_shows_deactivated_students_flagged(db, seeded):
    assert seeded["s_inactive"].id not in _rows(db, seeded["teacher"])
    rows = _rows(db, seeded["teacher"], include_inactive=True)
    assert rows[seeded["s_inactive"].id]["is_inactive"] is True


def test_other_teachers_students_never_leak(db, seeded):
    rows = _rows(db, seeded["teacher"], include_archived=True, include_inactive=True)
    assert seeded["s_foreign"].id not in rows
    assert set(rows) == {seeded["s_current"].id, seeded["s_archived"].id,
                         seeded["s_both"].id, seeded["s_inactive"].id}
