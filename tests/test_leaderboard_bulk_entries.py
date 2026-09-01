"""
The bulk leaderboard-entry endpoint upserts many manual entries in one request
(replacing the per-student round-trip the grid used to make on save). It must:
  - create rows for new (group, week, user) tuples,
  - update existing rows in place (no duplicates) on a second call,
  - authorize the group once and skip entries a curator does not own.

Savepoint-isolated; skips without Postgres.
"""
import pytest

from src.schemas.models import Group, GroupStudent, UserInDB
from src.gamification.models import LeaderboardEntry
from src.gamification.routes.leaderboard import (
    update_leaderboard_entries_bulk,
    BulkLeaderboardEntryInputSchema,
)
from src.gamification.schemas import LeaderboardEntryCreateSchema


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


def _user(db, email, role):
    u = UserInDB(email=email, name=email.split("@")[0], hashed_password="x",
                 role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


def _payload(group_id, week, entries):
    return BulkLeaderboardEntryInputSchema(
        entries=[LeaderboardEntryCreateSchema(group_id=group_id, week_number=week, **e)
                 for e in entries]
    )


def _entries(db, group_id, week):
    return {
        e.user_id: e
        for e in db.query(LeaderboardEntry).filter(
            LeaderboardEntry.group_id == group_id,
            LeaderboardEntry.week_number == week,
        ).all()
    }


def test_bulk_creates_and_upserts_entries(db):
    admin = _user(db, "bulk_admin@test.local", "admin")
    teacher = _user(db, "bulk_teacher@test.local", "teacher")
    group = Group(name="Bulk GE", program_type="general_english", teacher_id=teacher.id)
    db.add(group)
    db.flush()

    s1 = _user(db, "bulk_s1@test.local", "student")
    s2 = _user(db, "bulk_s2@test.local", "student")
    for s in (s1, s2):
        db.add(GroupStudent(group_id=group.id, student_id=s.id))
    db.flush()

    # First save: create both rows.
    res = update_leaderboard_entries_bulk(
        _payload(group.id, 1, [
            {"user_id": s1.id, "curator_hour": 5, "extra_points": 2},
            {"user_id": s2.id, "curator_hour": 3},
        ]),
        current_user=admin, db=db,
    )
    assert res["updated_count"] == 2
    rows = _entries(db, group.id, 1)
    assert rows[s1.id].curator_hour == 5
    assert rows[s1.id].extra_points == 2
    assert rows[s2.id].curator_hour == 3

    # Second save: update s1 in place, no duplicate row.
    res2 = update_leaderboard_entries_bulk(
        _payload(group.id, 1, [{"user_id": s1.id, "curator_hour": 9}]),
        current_user=admin, db=db,
    )
    assert res2["updated_count"] == 1
    rows2 = _entries(db, group.id, 1)
    assert len(rows2) == 2  # still two rows, not three
    assert rows2[s1.id].curator_hour == 9
    assert rows2[s1.id].extra_points == 2  # untouched field preserved


def test_bulk_skips_group_a_curator_does_not_own(db):
    owner = _user(db, "bulk_owner_cur@test.local", "curator")
    intruder = _user(db, "bulk_intruder_cur@test.local", "curator")
    group = Group(name="Bulk Owned", program_type="general_english", curator_id=owner.id)
    db.add(group)
    db.flush()
    student = _user(db, "bulk_owned_student@test.local", "student")
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()

    # A curator who does not own the group gets the entry silently skipped.
    res = update_leaderboard_entries_bulk(
        _payload(group.id, 1, [{"user_id": student.id, "curator_hour": 7}]),
        current_user=intruder, db=db,
    )
    assert res["updated_count"] == 0
    assert _entries(db, group.id, 1) == {}

    # The owner succeeds.
    res_ok = update_leaderboard_entries_bulk(
        _payload(group.id, 1, [{"user_id": student.id, "curator_hour": 7}]),
        current_user=owner, db=db,
    )
    assert res_ok["updated_count"] == 1
    assert _entries(db, group.id, 1)[student.id].curator_hour == 7
