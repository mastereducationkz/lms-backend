"""Updating an assignment must not silently sever its lesson (event) link.

The AssignmentBuilder edit flow historically sent no event_id/event_mapping,
and PUT /assignments/{id} overwrote event_id with NULL — homework "unlinked
by itself" after any edit. These tests pin the fixed semantics:
  * payload without event fields -> keep the existing link;
  * payload with an event_mapping entry -> move the link.
"""
import pytest
from datetime import datetime, timedelta

from src.schemas.models import UserInDB, Group, Event, EventGroup
from src.assignments.models import Assignment
from src.assignments.schemas import AssignmentCreateSchema
from src.assignments.routes.assignments import update_assignment
from src.utils.auth_utils import hash_password


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


def _teacher(db):
    u = UserInDB(email="ael-teacher@test.local", name="ael-teacher", role="teacher",
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _group(db, teacher_id):
    g = Group(name="AEL Group", teacher_id=teacher_id)
    db.add(g); db.flush(); return g


def _class_event(db, group_id, creator_id, days_ahead=3):
    start = datetime.utcnow() + timedelta(days=days_ahead)
    ev = Event(title="AEL Lesson", event_type="class", is_active=True,
               start_datetime=start, end_datetime=start + timedelta(hours=1),
               created_by=creator_id)
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group_id)); db.flush()
    return ev


def _linked_assignment(db, group, event):
    a = Assignment(
        title="AEL homework", assignment_type="free_text",
        content='{"question": "q"}', group_id=group.id, event_id=event.id,
        lesson_number=5, due_date=event.start_datetime, max_score=10,
    )
    db.add(a); db.flush(); return a


def _edit_payload(group_id, **overrides):
    """Mimics the AssignmentBuilder edit payload: no event_id/event_mapping."""
    data = dict(
        title="AEL homework (edited)",
        assignment_type="free_text",
        content={"question": "q"},
        max_score=10,
        group_id=group_id,
        group_ids=[group_id],
        due_date=None,
        lesson_number_mapping={},
        due_date_mapping={},
    )
    data.update(overrides)
    return AssignmentCreateSchema(**data)


def test_update_without_event_fields_preserves_link(db):
    teacher = _teacher(db)
    group = _group(db, teacher.id)
    event = _class_event(db, group.id, teacher.id)
    a = _linked_assignment(db, group, event)

    update_assignment(
        assignment_id=a.id,
        assignment_data=_edit_payload(group.id),
        current_user=teacher,
        db=db,
    )

    db.refresh(a)
    assert a.event_id == event.id
    assert a.title == "AEL homework (edited)"


def test_update_without_due_date_preserves_due_date(db):
    teacher = _teacher(db)
    group = _group(db, teacher.id)
    event = _class_event(db, group.id, teacher.id)
    a = _linked_assignment(db, group, event)
    original_due = a.due_date

    update_assignment(
        assignment_id=a.id,
        assignment_data=_edit_payload(group.id),
        current_user=teacher,
        db=db,
    )

    db.refresh(a)
    assert a.due_date == original_due


def test_update_without_group_id_preserves_group(db):
    teacher = _teacher(db)
    group = _group(db, teacher.id)
    event = _class_event(db, group.id, teacher.id)
    a = _linked_assignment(db, group, event)

    update_assignment(
        assignment_id=a.id,
        assignment_data=_edit_payload(None, group_ids=None),
        current_user=teacher,
        db=db,
    )

    db.refresh(a)
    assert a.group_id == group.id


def test_update_with_event_mapping_moves_link(db):
    teacher = _teacher(db)
    group = _group(db, teacher.id)
    old_event = _class_event(db, group.id, teacher.id, days_ahead=3)
    new_event = _class_event(db, group.id, teacher.id, days_ahead=7)
    a = _linked_assignment(db, group, old_event)

    update_assignment(
        assignment_id=a.id,
        assignment_data=_edit_payload(
            group.id,
            event_mapping={group.id: new_event.id},
            due_date_mapping={group.id: new_event.start_datetime},
        ),
        current_user=teacher,
        db=db,
    )

    db.refresh(a)
    assert a.event_id == new_event.id
    assert a.due_date == new_event.start_datetime
