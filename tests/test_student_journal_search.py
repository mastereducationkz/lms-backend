"""Journal search and archived-group visibility.

Search must match email as well as name, and must find a student whose only
group is archived — the active-only default exists to declutter browsing, not
to hide people. The archived toggle brings archived groups back explicitly.
"""
import pytest

from src.schemas.models import Group, GroupStudent, UserInDB  # noqa: F401 — shim first
from src.curator.routes.student_journal import (
    _build_student_query,
    _get_allowed_group_ids,
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


@pytest.fixture
def seeded(db):
    curator = UserInDB(email="journal.curator@test.local", name="Curator",
                       hashed_password="x", role="curator", is_active=True)
    active_student = UserInDB(email="journal.active@test.local", name="Актив Студентов",
                              hashed_password="x", role="student", is_active=True)
    archived_student = UserInDB(email="journal.archived@test.local", name="Архив Студентов",
                                hashed_password="x", role="student", is_active=True)
    db.add_all([curator, active_student, archived_student])
    db.flush()

    active_group = Group(name="Journal Active Group", curator_id=curator.id, is_active=True)
    archived_group = Group(name="Journal Archived Group", curator_id=curator.id, is_active=False)
    db.add_all([active_group, archived_group])
    db.flush()
    db.add(GroupStudent(group_id=active_group.id, student_id=active_student.id))
    db.add(GroupStudent(group_id=archived_group.id, student_id=archived_student.id))
    db.flush()
    return {"curator": curator, "active_student": active_student,
            "archived_student": archived_student}


def _ids(query):
    return {user.id for user, _, _ in query.all()}


def test_search_matches_email(db, seeded):
    q = _build_student_query(db, None, None, "journal.active@test")
    assert seeded["active_student"].id in _ids(q)


def test_default_view_hides_archived_but_search_finds_them(db, seeded):
    browsing = _build_student_query(db, None, None, None)
    assert seeded["archived_student"].id not in _ids(browsing)

    by_name = _build_student_query(db, None, None, "Архив Студентов")
    assert seeded["archived_student"].id in _ids(by_name)

    toggled = _build_student_query(db, None, None, None, include_archived=True)
    assert seeded["archived_student"].id in _ids(toggled)


def test_search_finds_deactivated_students(db, seeded):
    student = seeded["archived_student"]
    student.is_active = False
    db.flush()

    # Hidden from default browsing even with archived groups shown...
    browsing = _build_student_query(db, None, None, None, include_archived=True)
    assert student.id not in _ids(browsing)
    # ...but a search always finds them, and the explicit toggle shows them.
    by_email = _build_student_query(db, None, None, "journal.archived@test")
    assert student.id in _ids(by_email)
    toggled = _build_student_query(db, None, None, None,
                                   include_archived=True, include_inactive=True)
    assert student.id in _ids(toggled)


def test_curator_keeps_access_to_archived_groups(db, seeded):
    allowed = _get_allowed_group_ids(seeded["curator"], db)
    q = _build_student_query(db, allowed, None, None, include_archived=True)
    assert seeded["archived_student"].id in _ids(q)


def test_curator_opens_profile_of_archived_group_student(db, seeded):
    """The list shows archived-group students; the profile must open them too."""
    from src.curator.routes.student_journal import get_student_profile

    profile = get_student_profile(
        seeded["archived_student"].id, current_user=seeded["curator"], db=db,
    )
    assert profile["student"]["id"] == seeded["archived_student"].id


def test_foreign_curator_still_denied_profile(db, seeded):
    from fastapi import HTTPException
    from src.curator.routes.student_journal import get_student_profile

    other = UserInDB(email="journal.other.curator@test.local", name="Other C",
                     hashed_password="x", role="curator", is_active=True)
    db.add(other)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        get_student_profile(seeded["archived_student"].id, current_user=other, db=db)
    assert exc.value.status_code == 403
