"""Доступ к родительским отчётам и upsert по (ученик, неделя)."""
from datetime import date

import pytest
from fastapi import HTTPException


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


@pytest.fixture
def curator_and_group(db):
    from src.schemas.models import Group, GroupStudent, UserInDB
    curator = UserInDB(name="Айгерим", email="curator-pr@test.kz",
                       role="curator", hashed_password="x")
    stranger = UserInDB(name="Другой", email="curator2-pr@test.kz",
                        role="curator", hashed_password="x")
    student = UserInDB(name="Амир", email="amir-pr-routes@test.kz",
                       role="student", hashed_password="x")
    db.add_all([curator, stranger, student])
    db.flush()
    group = Group(name="SAT-3", program_type="sat", curator_id=curator.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()
    return curator, stranger, student, group


def test_curator_of_the_group_passes_group_check(db, curator_and_group):
    from src.reports.parent.routes import _require_group_access
    curator, _, _, group = curator_and_group
    assert _require_group_access(group.id, curator, db).id == group.id


def test_foreign_curator_is_rejected(db, curator_and_group):
    from src.reports.parent.routes import _require_group_access
    _, stranger, _, group = curator_and_group
    with pytest.raises(HTTPException) as exc:
        _require_group_access(group.id, stranger, db)
    assert exc.value.status_code == 403


def test_admin_passes_any_group(db, curator_and_group):
    from src.reports.parent.routes import _require_group_access
    from src.schemas.models import UserInDB
    _, _, _, group = curator_and_group
    admin = UserInDB(name="Админ", email="admin-pr@test.kz",
                     role="admin", hashed_password="x")
    db.add(admin)
    db.flush()
    assert _require_group_access(group.id, admin, db).id == group.id


def test_save_upserts_instead_of_duplicating(db, curator_and_group):
    from src.reports.parent.models import ParentReport
    from src.reports.parent.routes import _upsert
    curator, _, student, group = curator_and_group

    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t1", template_auto=True, facts={"a": 1},
            body_generated="первый", curator_note=None, user_id=curator.id)
    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t5", template_auto=False, facts={"a": 2},
            body_generated="второй", curator_note="заметка", user_id=curator.id)

    rows = db.query(ParentReport).filter(ParentReport.student_id == student.id).all()
    assert len(rows) == 1
    assert rows[0].template_key == "t5"
    assert rows[0].body_generated == "второй"
    assert rows[0].facts_json == {"a": 2}


def test_curator_edit_survives_but_regeneration_replaces_it(db, curator_and_group):
    from src.reports.parent.models import ParentReport
    from src.reports.parent.routes import _upsert
    curator, _, student, group = curator_and_group

    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t1", template_auto=True, facts={}, body_generated="исходный",
            curator_note=None, user_id=curator.id)
    row = db.query(ParentReport).filter(ParentReport.student_id == student.id).one()
    row.body = "поправленный куратором"
    db.flush()

    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t1", template_auto=True, facts={}, body_generated="новый",
            curator_note=None, user_id=curator.id)
    row = db.query(ParentReport).filter(ParentReport.student_id == student.id).one()
    assert row.body == "новый"
    assert row.body_generated == "новый"
