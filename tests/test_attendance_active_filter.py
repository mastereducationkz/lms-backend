import pytest
from datetime import datetime, timedelta
from src.schemas.models import UserInDB, Group, GroupStudent, Event, EventGroup
from src.events.models import Attendance
from src.utils.auth_utils import hash_password
from src.admin.routes.dashboard import _missing_attendance_reminders


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


def _u(db, email, role="student"):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _past_class(db, group_id, creator_id):
    past = datetime.utcnow() - timedelta(days=5)  # after Feb-16 cutoff, in the past
    ev = Event(title="Lesson", event_type="class", is_active=True,
               start_datetime=past, end_datetime=past + timedelta(hours=1), created_by=creator_id)
    db.add(ev); db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=group_id)); db.flush()
    return ev


def test_hides_groups_without_recent_attendance(db):
    t = _u(db, "aaf-t@test.local", "teacher")
    s1 = _u(db, "aaf-s1@test.local")
    s2 = _u(db, "aaf-s2@test.local")
    s3 = _u(db, "aaf-s3@test.local")
    gActive = Group(name="AAF Active", is_active=True)
    gStale = Group(name="AAF Stale", is_active=True)
    db.add_all([gActive, gStale]); db.flush()
    db.add_all([
        GroupStudent(group_id=gActive.id, student_id=s1.id),
        GroupStudent(group_id=gActive.id, student_id=s2.id),
        GroupStudent(group_id=gStale.id, student_id=s3.id),
    ]); db.flush()

    # gActive: past class with only 1 of 2 marked -> flagged; the mark is recent -> group is "active".
    eA = _past_class(db, gActive.id, t.id)
    db.add(Attendance(event_id=eA.id, user_id=s1.id, status="present")); db.flush()
    # gStale: past class, nobody marked, no attendance anywhere -> inactive.
    _past_class(db, gStale.id, t.id)
    db.flush()

    res = _missing_attendance_reminders(db, group_ids=[gActive.id, gStale.id])
    gids = {r["group_id"] for r in res}
    assert gActive.id in gids     # actively marking -> shown
    assert gStale.id not in gids  # no attendance in 21d -> hidden
