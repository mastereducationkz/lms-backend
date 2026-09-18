"""A lesson covered by somebody else says who taught it, and whose group it is.

The calendar told everyone who was not the group's owner «You are substituting» — true only for
the substitute, wrong for the admins, head teachers and curators who read the calendar all day
(reported 2026-09-18). The payload only ever carried the teacher's name, never the owner's, so the
page had nothing better to say.
"""
from datetime import datetime, timedelta

import pytest

from src.events.schemas import EventSchema
from src.schemas.models import Event, EventGroup, Group, UserInDB


@pytest.fixture
def db():
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


def _user(db, name):
    from src.utils.auth_utils import hash_password
    user = UserInDB(email=f"cal-{datetime.now().timestamp():.6f}@test.local", name=name,
                    role="teacher", hashed_password=hash_password("x"), is_active=True)
    db.add(user)
    db.flush()
    return user


def _lesson(db, *, owner, taught_by):
    group = Group(name="NUET August 1 2026 - Ақжол", teacher_id=owner.id, is_active=True,
                  is_over=False, program_type="nuet")
    db.add(group)
    db.flush()
    event = Event(title="NUET, урок", event_type="class",
                  start_datetime=datetime(2026, 9, 24, 13, 0),
                  end_datetime=datetime(2026, 9, 24, 14, 0), created_by=owner.id,
                  teacher_id=taught_by.id, is_active=True)
    db.add(event)
    db.flush()
    db.add(EventGroup(event_id=event.id, group_id=group.id))
    db.flush()
    db.refresh(event)
    return event


def test_a_covered_lesson_names_the_teacher_and_the_group_owner(db):
    owner, substitute = _user(db, "Орынбасар Ақжол"), _user(db, "Қайратқызы Дина")
    event = _lesson(db, owner=owner, taught_by=substitute)

    assert event.is_substitution is True
    assert event.teacher_name == "Қайратқызы Дина"
    assert event.group_teacher_name == "Орынбасар Ақжол"

    payload = EventSchema.model_validate(event)
    assert payload.teacher_name == "Қайратқызы Дина"
    assert payload.group_teacher_name == "Орынбасар Ақжол"


def test_an_ordinary_lesson_is_not_a_substitution(db):
    owner = _user(db, "Орынбасар Ақжол")
    event = _lesson(db, owner=owner, taught_by=owner)
    assert event.is_substitution is False
    assert event.group_teacher_name == "Орынбасар Ақжол"


def test_a_lesson_with_no_group_has_no_owner_to_name(db):
    teacher = _user(db, "Преподаватель")
    event = Event(title="Консультация", event_type="class",
                  start_datetime=datetime(2026, 9, 24, 13, 0),
                  end_datetime=datetime(2026, 9, 24, 14, 0), created_by=teacher.id,
                  teacher_id=teacher.id, is_active=True)
    db.add(event)
    db.flush()
    db.refresh(event)
    assert event.group_teacher_name is None
    assert event.is_substitution is False
