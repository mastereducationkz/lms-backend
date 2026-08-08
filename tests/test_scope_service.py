"""Row-scope service: which groups/students each staff role may see.

Before this service existed, group scope was re-derived inline at ~40 teacher sites
and ~45 curator sites, with seven mutually-inconsistent shapes (some filtering
Group.is_active, some not). Every new exam-results and Bluebook endpoint reads scope
from here instead, so there is exactly one answer per role.

Head-teacher scope is deliberately COURSE-LINK scope (CourseHeadTeacher ->
CourseGroupAccess), matching check_group_access / check_student_access. The wider
program_type scope used by the leaderboard and lesson-requests is intentionally NOT
used for these PII-bearing reads.

Real-Postgres SAVEPOINT fixture, same pattern as tests/test_weekly_lessons_teacher_access.py.
"""
import pytest

from src.schemas.models import (
    Course,
    CourseGroupAccess,
    CourseHeadTeacher,
    Group,
    GroupStudent,
    UserInDB,
)
from src.utils.scope import (
    UNRESTRICTED,
    can_view_group,
    visible_group_ids,
    visible_student_ids,
)


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


def _user(db, role, email):
    u = UserInDB(email=email, name=f"{role}-{email}", hashed_password="x",
                 role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


def _group(db, name, *, teacher_id=None, curator_id=None,
           is_active=True, is_over=False, program_type="sat"):
    g = Group(name=name, teacher_id=teacher_id, curator_id=curator_id,
              is_active=is_active, is_over=is_over, program_type=program_type)
    db.add(g)
    db.flush()
    return g


def _enrol(db, group, student):
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()


# --------------------------------------------------------------------------------------
# Unrestricted roles
# --------------------------------------------------------------------------------------

def test_admin_is_unrestricted(db):
    admin = _user(db, "admin", "scope-admin@t.io")
    assert visible_group_ids(admin, db) is UNRESTRICTED


def test_head_curator_is_unrestricted(db):
    hc = _user(db, "head_curator", "scope-hc@t.io")
    assert visible_group_ids(hc, db) is UNRESTRICTED


# --------------------------------------------------------------------------------------
# Teacher
# --------------------------------------------------------------------------------------

def test_teacher_sees_only_own_groups(db):
    mine = _user(db, "teacher", "scope-t1@t.io")
    theirs = _user(db, "teacher", "scope-t2@t.io")
    g_mine = _group(db, "scope Mine", teacher_id=mine.id)
    g_theirs = _group(db, "scope Theirs", teacher_id=theirs.id)

    got = visible_group_ids(mine, db)
    assert g_mine.id in got
    assert g_theirs.id not in got


def test_teacher_scope_excludes_archived_groups(db):
    """permissions.py:88 omits is_active, so archived groups leaked. We filter."""
    t = _user(db, "teacher", "scope-t3@t.io")
    live = _group(db, "scope Live", teacher_id=t.id)
    archived = _group(db, "scope Archived", teacher_id=t.id, is_active=False)

    got = visible_group_ids(t, db)
    assert live.id in got
    assert archived.id not in got


def test_teacher_scope_excludes_finished_groups(db):
    t = _user(db, "teacher", "scope-t4@t.io")
    live = _group(db, "scope Live2", teacher_id=t.id)
    over = _group(db, "scope Over", teacher_id=t.id, is_over=True)

    got = visible_group_ids(t, db)
    assert live.id in got
    assert over.id not in got


def test_teacher_with_no_groups_gets_empty_not_unrestricted(db):
    """An empty list must never be confused with UNRESTRICTED."""
    t = _user(db, "teacher", "scope-t5@t.io")
    got = visible_group_ids(t, db)
    assert got is not UNRESTRICTED
    assert got == []


# --------------------------------------------------------------------------------------
# Curator
# --------------------------------------------------------------------------------------

def test_curator_sees_only_own_groups(db):
    mine = _user(db, "curator", "scope-c1@t.io")
    theirs = _user(db, "curator", "scope-c2@t.io")
    g_mine = _group(db, "scope CMine", curator_id=mine.id)
    g_theirs = _group(db, "scope CTheirs", curator_id=theirs.id)

    got = visible_group_ids(mine, db)
    assert g_mine.id in got
    assert g_theirs.id not in got


def test_curator_does_not_inherit_teacher_groups(db):
    """A curator_id match must not be satisfied by a teacher_id match."""
    c = _user(db, "curator", "scope-c3@t.io")
    taught_not_curated = _group(db, "scope TNC", teacher_id=c.id)
    assert taught_not_curated.id not in visible_group_ids(c, db)


# --------------------------------------------------------------------------------------
# Head teacher - COURSE-LINK scope (Scope A)
# --------------------------------------------------------------------------------------

def _managed_course_with_group(db, head, course_name, group):
    course = Course(title=course_name, description="d", teacher_id=head.id)
    db.add(course)
    db.flush()
    db.add(CourseHeadTeacher(course_id=course.id, head_teacher_id=head.id))
    db.add(CourseGroupAccess(course_id=course.id, group_id=group.id,
                             granted_by=head.id, is_active=True))
    db.flush()
    return course


def test_head_teacher_sees_groups_linked_to_managed_courses(db):
    head = _user(db, "head_teacher", "scope-ht1@t.io")
    linked = _group(db, "scope HTLinked")
    _managed_course_with_group(db, head, "scope Course A", linked)

    assert linked.id in visible_group_ids(head, db)


def test_head_teacher_does_not_see_unlinked_group_of_same_program(db):
    """The decisive difference from program_type scope: managing one SAT course must
    NOT grant every SAT group platform-wide."""
    head = _user(db, "head_teacher", "scope-ht2@t.io")
    linked = _group(db, "scope HTLinked2", program_type="sat")
    unlinked = _group(db, "scope HTUnlinked", program_type="sat")
    _managed_course_with_group(db, head, "scope Course B", linked)

    got = visible_group_ids(head, db)
    assert linked.id in got
    assert unlinked.id not in got


def test_head_teacher_with_no_managed_courses_sees_nothing(db):
    head = _user(db, "head_teacher", "scope-ht3@t.io")
    _group(db, "scope Orphan", program_type="sat")
    assert visible_group_ids(head, db) == []


def test_head_teacher_scope_excludes_archived_groups(db):
    head = _user(db, "head_teacher", "scope-ht4@t.io")
    archived = _group(db, "scope HTArchived", is_active=False)
    _managed_course_with_group(db, head, "scope Course C", archived)
    assert archived.id not in visible_group_ids(head, db)


# --------------------------------------------------------------------------------------
# Non-staff roles get nothing
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["student", "parent"])
def test_non_staff_roles_have_no_group_scope(db, role):
    u = _user(db, role, f"scope-{role}@t.io")
    got = visible_group_ids(u, db)
    assert got is not UNRESTRICTED
    assert got == []


def test_unknown_role_fails_closed(db):
    """An unrecognised role must get nothing, never UNRESTRICTED."""
    u = _user(db, "marketing", "scope-unknown@t.io")
    got = visible_group_ids(u, db)
    assert got is not UNRESTRICTED
    assert got == []


# --------------------------------------------------------------------------------------
# can_view_group
# --------------------------------------------------------------------------------------

def test_can_view_group_allows_own_and_denies_foreign(db):
    mine = _user(db, "teacher", "scope-cv1@t.io")
    theirs = _user(db, "teacher", "scope-cv2@t.io")
    g_mine = _group(db, "scope CVMine", teacher_id=mine.id)
    g_theirs = _group(db, "scope CVTheirs", teacher_id=theirs.id)

    assert can_view_group(mine, db, g_mine.id) is True
    assert can_view_group(mine, db, g_theirs.id) is False


def test_can_view_group_true_for_admin_on_any_group(db):
    admin = _user(db, "admin", "scope-cv3@t.io")
    g = _group(db, "scope CVAny")
    assert can_view_group(admin, db, g.id) is True


# --------------------------------------------------------------------------------------
# visible_student_ids
# --------------------------------------------------------------------------------------

def test_visible_student_ids_covers_only_students_in_scoped_groups(db):
    t = _user(db, "teacher", "scope-s1@t.io")
    other = _user(db, "teacher", "scope-s2@t.io")
    g_mine = _group(db, "scope SMine", teacher_id=t.id)
    g_theirs = _group(db, "scope STheirs", teacher_id=other.id)

    a = _user(db, "student", "scope-stu-a@t.io")
    b = _user(db, "student", "scope-stu-b@t.io")
    _enrol(db, g_mine, a)
    _enrol(db, g_theirs, b)

    got = visible_student_ids(t, db)
    assert a.id in got
    assert b.id not in got


def test_visible_student_ids_unrestricted_for_admin(db):
    admin = _user(db, "admin", "scope-s3@t.io")
    assert visible_student_ids(admin, db) is UNRESTRICTED


def test_visible_student_ids_deduplicates_across_groups(db):
    """A student in two of the teacher's groups must appear once."""
    t = _user(db, "teacher", "scope-s4@t.io")
    g1 = _group(db, "scope Dup1", teacher_id=t.id)
    g2 = _group(db, "scope Dup2", teacher_id=t.id)
    stu = _user(db, "student", "scope-stu-dup@t.io")
    _enrol(db, g1, stu)
    _enrol(db, g2, stu)

    got = visible_student_ids(t, db)
    assert got.count(stu.id) == 1
