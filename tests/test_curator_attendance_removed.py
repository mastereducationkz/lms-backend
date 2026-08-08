"""Curators may no longer mark attendance.

Product decision: marking attendance is the teacher's record of their own lesson. A
curator editing it silently rewrites someone else's register, so `curator` is removed
from every attendance WRITE path. Curators keep full READ access to the leaderboard.

There are THREE write paths, and closing only the one behind the leaderboard UI would
leave the capability reachable through the events API:

    POST /leaderboard/curator/attendance        (single)
    POST /leaderboard/curator/attendance/bulk   (bulk)
    POST /events/{event_id}/attendance          (event roster)

`head_curator` deliberately keeps the capability, and teacher / admin / head_teacher
must be unaffected - so the accept cases are asserted too, not just the rejects. This
mirrors tests/test_curator_grading_removed.py, which made the same call for grading.

Source-level assertions are used for the role gates so they run green without Postgres
(a third of this suite skips silently when no database is reachable).
"""
import inspect

import pytest

from src.gamification.routes.leaderboard import update_attendance, update_attendance_bulk
from src.events.routes.events import update_event_attendance


def _gate_source(fn) -> str:
    return inspect.getsource(fn)


# --------------------------------------------------------------------------------------
# Leaderboard: single attendance
# --------------------------------------------------------------------------------------

def test_single_attendance_role_gate_excludes_curator():
    src = _gate_source(update_attendance)
    gate = next(l for l in src.splitlines() if "current_user.role not in [" in l)
    assert '"curator"' not in gate, gate


def test_single_attendance_role_gate_keeps_the_other_staff_roles():
    gate = next(
        l for l in _gate_source(update_attendance).splitlines()
        if "current_user.role not in [" in l
    )
    for role in ("admin", "head_curator", "teacher", "head_teacher"):
        assert f'"{role}"' in gate, f"{role} must keep attendance rights: {gate}"


# --------------------------------------------------------------------------------------
# Leaderboard: bulk attendance
# --------------------------------------------------------------------------------------

def test_bulk_attendance_role_gate_excludes_curator():
    gate = next(
        l for l in _gate_source(update_attendance_bulk).splitlines()
        if "current_user.role not in [" in l
    )
    assert '"curator"' not in gate, gate


def test_bulk_attendance_role_gate_keeps_the_other_staff_roles():
    gate = next(
        l for l in _gate_source(update_attendance_bulk).splitlines()
        if "current_user.role not in [" in l
    )
    for role in ("admin", "teacher", "head_teacher", "head_curator"):
        assert f'"{role}"' in gate, f"{role} must keep attendance rights: {gate}"


# --------------------------------------------------------------------------------------
# Events: the third path, easy to miss
# --------------------------------------------------------------------------------------

def test_event_attendance_rejects_curator_explicitly():
    """The shared require_teacher_curator_or_admin dependency still admits curators
    because it guards read paths too, so the endpoint must reject the role itself."""
    src = _gate_source(update_event_attendance)
    assert 'role or ""' in src and '== "curator"' in src, src
    assert "Curators cannot mark attendance" in src


def test_event_attendance_curator_rejection_precedes_any_write():
    """The rejection must come before the bulk upsert, not after it."""
    src = _gate_source(update_event_attendance)
    assert src.index('== "curator"') < src.index("bulk_upsert_for_event")


# --------------------------------------------------------------------------------------
# Curators keep read access - the point is read-only, not locked out
# --------------------------------------------------------------------------------------

def test_curator_still_reads_the_leaderboard():
    from src.gamification.routes.leaderboard import get_curator_groups
    src = inspect.getsource(get_curator_groups)
    assert "curator" in src


def test_weekly_lessons_grid_still_available_to_curators():
    from src.gamification.routes.leaderboard import get_weekly_lessons_with_hw_status
    src = inspect.getsource(get_weekly_lessons_with_hw_status)
    assert "curator" in src


# --------------------------------------------------------------------------------------
# Live rejection (needs Postgres)
# --------------------------------------------------------------------------------------

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


def test_curator_gets_403_from_the_single_attendance_endpoint(db):
    from fastapi import HTTPException
    from src.schemas.models import Group, UserInDB
    from src.gamification.routes.leaderboard import AttendanceInputSchema

    curator = UserInDB(email="att-cur@t.io", name="Att Curator", hashed_password="x",
                       role="curator", is_active=True)
    db.add(curator)
    db.flush()
    group = Group(name="att Group", curator_id=curator.id, is_active=True,
                  is_over=False, program_type="sat")
    db.add(group)
    db.flush()

    # Their OWN group - the rejection is about the role, not about scope.
    payload = AttendanceInputSchema(group_id=group.id, student_id=curator.id,
                                    week_number=1, lesson_index=1, score=1)
    with pytest.raises(HTTPException) as exc:
        update_attendance(data=payload, current_user=curator, db=db)
    assert exc.value.status_code == 403
