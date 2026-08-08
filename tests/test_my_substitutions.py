"""
Tests for GET /events/my-substitutions.

Real Postgres session with SAVEPOINT rollback; endpoint invoked directly.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.schemas.models import Event, EventGroup, Group, UserInDB
from src.events.routes.events import get_my_substitutions


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


def _user(db, email, role="teacher"):
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x", role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


def _class_event(db, group_id, created_by, teacher_id, start=None, event_type="class"):
    start = start or datetime.now(timezone.utc).replace(microsecond=0)
    ev = Event(
        title="G: Lesson", event_type=event_type, start_datetime=start,
        end_datetime=start + timedelta(hours=1), created_by=created_by,
        teacher_id=teacher_id, is_active=True, is_recurring=False,
    )
    db.add(ev)
    db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group_id))
    db.flush()
    return ev


def test_returns_only_genuine_substitutions(db):
    owner = _user(db, "ms_owner@test.local")
    sub = _user(db, "ms_sub@test.local")
    other = _user(db, "ms_other@test.local")

    group = Group(name="MS Group", teacher_id=owner.id)
    db.add(group)
    db.flush()

    # genuine substitution: event teacher = sub, group owned by someone else
    sub_ev = _class_event(db, group.id, owner.id, teacher_id=sub.id)
    # sub's own owned-group lesson (not a substitution)
    own_group = Group(name="MS Own", teacher_id=sub.id)
    db.add(own_group)
    db.flush()
    own_ev = _class_event(db, own_group.id, sub.id, teacher_id=sub.id)
    # another teacher's substitution
    other_ev = _class_event(db, group.id, owner.id, teacher_id=other.id)

    res = get_my_substitutions(current_user=sub, db=db)
    ids = {r.event_id for r in res}
    assert sub_ev.id in ids
    assert own_ev.id not in ids
    assert other_ev.id not in ids

    row = next(r for r in res if r.event_id == sub_ev.id)
    assert row.group_id == group.id
    assert row.group_name == "MS Group"
    # Original teacher = the group's regular teacher (who sub is covering for).
    assert row.original_teacher_name == "ms_owner"
    # No attendance recorded yet.
    assert row.marked is False


def test_marked_reflects_attendance(db):
    from src.events.models import Attendance

    owner = _user(db, "ms3_owner@test.local")
    sub = _user(db, "ms3_sub@test.local")
    student = _user(db, "ms3_student@test.local", role="student")
    group = Group(name="MS3 Group", teacher_id=owner.id)
    db.add(group)
    db.flush()

    marked_ev = _class_event(db, group.id, owner.id, teacher_id=sub.id)
    unmarked_ev = _class_event(db, group.id, owner.id, teacher_id=sub.id,
                               start=datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1))
    db.add(Attendance(event_id=marked_ev.id, user_id=student.id, status="present"))
    db.flush()

    res = {r.event_id: r for r in get_my_substitutions(current_user=sub, db=db)}
    assert res[marked_ev.id].marked is True
    assert res[unmarked_ev.id].marked is False


def test_excludes_non_class_and_out_of_window(db):
    owner = _user(db, "ms2_owner@test.local")
    sub = _user(db, "ms2_sub@test.local")
    group = Group(name="MS2 Group", teacher_id=owner.id)
    db.add(group)
    db.flush()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    webinar = _class_event(db, group.id, owner.id, teacher_id=sub.id, event_type="webinar")
    old = _class_event(db, group.id, owner.id, teacher_id=sub.id, start=now - timedelta(days=200))

    res = get_my_substitutions(current_user=sub, db=db)
    ids = {r.event_id for r in res}
    assert webinar.id not in ids
    assert old.id not in ids


def test_my_substitutions_route_not_shadowed_by_event_id():
    from starlette.routing import Match
    from src.events.routes.events import router

    scope = {"type": "http", "method": "GET", "path": "/my-substitutions", "path_params": {}}
    matched = None
    for r in router.routes:
        m, _ = r.matches(scope)
        if m == Match.FULL:
            matched = r.path
            break
    assert matched == "/my-substitutions", f"/my-substitutions is shadowed by {matched}"
