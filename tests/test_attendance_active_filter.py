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


def _member(db, group_id, student_id, joined_at=None):
    """Add a student to a group, optionally backdating GroupStudent.created_at so
    tests can control whether the student counts as a member as of a given event
    (the default ORM value is "now", which is *after* any backdated past event)."""
    gs = GroupStudent(group_id=group_id, student_id=student_id)
    if joined_at is not None:
        gs.created_at = joined_at
    db.add(gs); db.flush(); return gs


def _past_class(db, group_id, creator_id, days_ago=5):
    past = datetime.utcnow() - timedelta(days=days_ago)  # after Feb-16 cutoff, in the past
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
    joined_long_ago = datetime.utcnow() - timedelta(days=30)
    _member(db, gActive.id, s1.id, joined_at=joined_long_ago)
    _member(db, gActive.id, s2.id, joined_at=joined_long_ago)
    _member(db, gStale.id, s3.id, joined_at=joined_long_ago)

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


def test_expected_count_is_join_date_aware(db):
    """A student added to a group after a past lesson must not inflate that
    lesson's expected headcount — otherwise a fully-marked historical lesson gets
    flagged as "missing attendance" forever just because someone joined later."""
    t = _u(db, "jda-t@test.local", "teacher")
    s1 = _u(db, "jda-s1@test.local")
    s2 = _u(db, "jda-s2@test.local")
    late_joiner = _u(db, "jda-s3@test.local")
    g = Group(name="JDA Group", is_active=True)
    db.add(g); db.flush()

    joined_long_ago = datetime.utcnow() - timedelta(days=30)
    _member(db, g.id, s1.id, joined_at=joined_long_ago)
    _member(db, g.id, s2.id, joined_at=joined_long_ago)

    # Lesson E happened 10 days ago and was fully marked for the 2 members at the time.
    eE = _past_class(db, g.id, t.id, days_ago=10)
    db.add_all([
        Attendance(event_id=eE.id, user_id=s1.id, status="present"),
        Attendance(event_id=eE.id, user_id=s2.id, status="present"),
    ]); db.flush()

    # Recent attendance mark keeps the group "active" so it isn't hidden by the 21d filter.
    eRecent = _past_class(db, g.id, t.id, days_ago=1)
    db.add_all([
        Attendance(event_id=eRecent.id, user_id=s1.id, status="present"),
        Attendance(event_id=eRecent.id, user_id=s2.id, status="present"),
    ]); db.flush()

    # A 3rd student joins AFTER lesson E (5 days ago, lesson was 10 days ago).
    _member(db, g.id, late_joiner.id, joined_at=datetime.utcnow() - timedelta(days=5))
    db.flush()

    res = _missing_attendance_reminders(db, group_ids=[g.id])
    flagged_event_ids = {r["event_id"] for r in res}
    assert eE.id not in flagged_event_ids, (
        "lesson fully marked for its members at the time must not be flagged just "
        "because a student joined afterwards"
    )

    # A student who joined BEFORE an unmarked event must still count as expected.
    # late_joiner joined 5 days ago; this event happened 3 days ago (after the join).
    eUnmarked = _past_class(db, g.id, t.id, days_ago=3)
    res2 = _missing_attendance_reminders(db, group_ids=[g.id])
    flagged2 = {r["event_id"]: r for r in res2}
    assert eUnmarked.id in flagged2
    assert flagged2[eUnmarked.id]["expected_students"] == 3  # s1, s2, late_joiner all joined before it
