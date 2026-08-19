"""get_teacher_request_stats: per-teacher monthly request counts, threshold ≥ min_count."""
import pytest
from datetime import datetime
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession

from src.schemas.models import UserInDB, Group, LessonRequest
from src.utils.auth_utils import hash_password
from src.lesson_requests.services import get_teacher_request_stats


@pytest.fixture
def db():
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


def _u(db, email, role="teacher"):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _g(db, teacher_id):
    g = Group(name="G", is_active=True, is_over=False,
              teacher_id=teacher_id, program_type="sat")
    db.add(g); db.flush(); return g


def _req(db, requester_id, group_id, rtype, dt, status="pending"):
    r = LessonRequest(request_type=rtype, status=status, requester_id=requester_id,
                      group_id=group_id, original_datetime=dt)
    db.add(r); db.flush(); return r


def test_counts_all_types_threshold_two(db):
    t = _u(db, "stats-a@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 8, 3, 10, 0))
    _req(db, t.id, g.id, "substitution", datetime(2026, 8, 20, 10, 0))
    rows = get_teacher_request_stats(db, 2026, 8, min_count=2)
    mine = [r for r in rows if r["teacher_id"] == t.id]
    assert len(mine) == 1
    assert mine[0]["total"] == 2
    assert mine[0]["by_type"] == {"substitution": 1, "reschedule": 1, "cancel": 0}


def test_below_threshold_excluded(db):
    t = _u(db, "stats-b@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 8, 3, 10, 0))
    rows = get_teacher_request_stats(db, 2026, 8, min_count=2)
    assert all(r["teacher_id"] != t.id for r in rows)


def test_month_boundary_uses_original_datetime(db):
    t = _u(db, "stats-c@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 8, 31, 23, 0))   # in August
    _req(db, t.id, g.id, "reschedule", datetime(2026, 9, 1, 0, 30))    # in September
    aug = [r for r in get_teacher_request_stats(db, 2026, 8, min_count=1) if r["teacher_id"] == t.id]
    sep = [r for r in get_teacher_request_stats(db, 2026, 9, min_count=1) if r["teacher_id"] == t.id]
    assert aug and aug[0]["total"] == 1
    assert sep and sep[0]["total"] == 1


def test_december_rolls_over_to_january(db):
    t = _u(db, "stats-d@test.local")
    g = _g(db, t.id)
    _req(db, t.id, g.id, "reschedule", datetime(2026, 12, 15, 10, 0))
    _req(db, t.id, g.id, "reschedule", datetime(2027, 1, 2, 10, 0))
    dec = [r for r in get_teacher_request_stats(db, 2026, 12, min_count=1) if r["teacher_id"] == t.id]
    assert dec and dec[0]["total"] == 1


def test_scope_filter_excludes_out_of_scope_group(db):
    t = _u(db, "stats-e@test.local")
    g_in = _g(db, t.id)
    g_out = _g(db, t.id)
    _req(db, t.id, g_in.id, "reschedule", datetime(2026, 8, 3, 10, 0))
    _req(db, t.id, g_out.id, "reschedule", datetime(2026, 8, 4, 10, 0))
    rows = get_teacher_request_stats(db, 2026, 8, min_count=1, scope_group_ids=[g_in.id])
    mine = [r for r in rows if r["teacher_id"] == t.id]
    assert mine and mine[0]["total"] == 1


def test_empty_scope_returns_nothing(db):
    rows = get_teacher_request_stats(db, 2026, 8, min_count=1, scope_group_ids=[])
    assert rows == []
