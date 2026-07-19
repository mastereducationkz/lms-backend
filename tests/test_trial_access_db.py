"""End-to-end trial enforcement against a real Postgres (local only; auto-skips).

Uses the lms-postgres docker container's lms_test DB when reachable:
  TRIAL_TEST_DB_URL=postgresql://myuser:mypassword@localhost:5432/lms_test   (default)

Covers two layers:
  1. The pure enforcement matrix (trial_course_ids / trial_lesson_access) against
     real rows — mirrors the brief's skeleton.
  2. The /trials route handlers (create_trial, update_trial, revoke_trial,
     convert_trial) called directly as functions with a real DB session, covering
     the state-machine edges called out in Task 4's review carry-forward: the
     create-duplicate/stale-window/re-activation-conflict/revoke-convert 409s,
     and password-retention-vs-rotation on create.

Fixture strategy — SAVEPOINT isolation, not connection-level rollback:
    The route handlers under test call db.commit() internally (create_trial,
    update_trial, revoke_trial, convert_trial all commit their own changes), so a
    plain `connection.begin(); ...; txn.rollback()` fixture does NOT isolate —
    a nested `db.commit()` would just commit the outer transaction outright.
    Instead we open one connection-level transaction, bind a Session to that
    connection, and open a SAVEPOINT (`session.begin_nested()`) before yielding.
    A SQLAlchemy `after_transaction_end` listener restarts a fresh SAVEPOINT every
    time the previous one ends (i.e. every time a handler's db.commit() releases
    it), so every handler commit nests inside the outer transaction. Rolling back
    the outer transaction at teardown erases everything, real commits included.
    This is the same pattern already used by tests/test_lesson_topic.py.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException


URL = os.getenv("TRIAL_TEST_DB_URL", "postgresql://myuser:mypassword@localhost:5432/lms_test")


def _engine_or_none():
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(URL, pool_pre_ping=True, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return eng
    except Exception:
        return None


ENGINE = _engine_or_none()
pytestmark = pytest.mark.skipif(ENGINE is None, reason="local test Postgres not reachable")

if ENGINE is not None:
    # Registers every domain model onto Base.metadata; safe/idempotent against the
    # disposable lms_test DB (create_all only fills in missing tables, never alters
    # existing ones — the trial tables/columns here are expected to already exist
    # via `alembic upgrade head`, same as any other migrated column).
    from src.models.base import Base
    import src.schemas.models  # noqa: F401
    Base.metadata.create_all(bind=ENGINE)


@pytest.fixture()
def db(monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker
    from src.trials.routes import trials as trials_routes

    # Route handlers only ever *queue* this via background_tasks.add_task, which
    # we never execute in these tests (we call handlers directly, not through the
    # ASGI app) — but monkeypatch it anyway per the brief so nothing can slip a
    # real email through if that ever changes.
    monkeypatch.setattr(trials_routes, "send_invite_email", lambda *a, **k: None)

    conn = ENGINE.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn)
    s = Session()
    s.begin_nested()

    @event.listens_for(s, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.expire_all()
            sess.begin_nested()

    try:
        yield s
    finally:
        event.remove(s, "after_transaction_end", _restart_savepoint)
        s.close()
        txn.rollback()
        conn.close()


# --- seed helpers --------------------------------------------------------------

def _future(hours=24):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _admin(db, email="trial-e2e-admin@x.kz"):
    from src.schemas.models import UserInDB
    u = UserInDB(email=email, name="Admin", hashed_password="x", role="admin", is_active=True)
    db.add(u)
    db.flush()
    return u


def _trial_user(db, email, hashed_password="x"):
    from src.schemas.models import UserInDB
    u = UserInDB(email=email, name="Prospect", hashed_password=hashed_password, role="student", is_trial=True)
    db.add(u)
    db.flush()
    return u


def _course_with_lessons(db, title, n=2):
    from src.schemas.models import Course, Module, Lesson
    c = Course(title=title, teacher_id=None, is_active=True)
    db.add(c)
    db.flush()
    m = Module(course_id=c.id, title="M", order_index=0)
    db.add(m)
    db.flush()
    lessons = []
    for i in range(n):
        l = Lesson(module_id=m.id, title=f"L{i}", order_index=i)
        db.add(l)
        lessons.append(l)
    db.flush()
    return c, m, lessons


def _make_grant(db, user, course, lesson_ids, status="active", expires_delta=timedelta(hours=24), granted_by=None):
    from src.trials.models import TrialAccess
    from src.trials.services import utcnow
    g = TrialAccess(
        user_id=user.id,
        course_id=course.id,
        lesson_ids=list(lesson_ids),
        expires_at=utcnow() + expires_delta,
        status=status,
        granted_by=granted_by.id if granted_by else None,
    )
    db.add(g)
    db.flush()
    return g


# --- 1. pure enforcement matrix (brief's skeleton, adapted) --------------------

def test_enforcement_matrix_db(db):
    from src.trials.services import trial_lesson_access, trial_course_ids, utcnow

    u = _trial_user(db, "trial-e2e-matrix@x.kz")
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-matrix-course")
    g = _make_grant(db, u, c, [l1.id])

    assert trial_course_ids(db, u.id) == [c.id]
    assert trial_lesson_access(db, u.id, l1.id) == (True, None)
    ok, reason = trial_lesson_access(db, u.id, l2.id)
    assert ok is False and reason == "Not included in your trial"

    g.expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    ok, reason = trial_lesson_access(db, u.id, l1.id)
    assert ok is False and reason == "Your trial has ended"
    assert trial_course_ids(db, u.id) == []


# --- 2. route-level state machine (Task 4 review carry-forward) ----------------

def test_create_trial_duplicate_active_returns_409(db):
    from src.trials.routes.trials import create_trial, _DUPLICATE_ACTIVE_DETAIL
    from src.trials.schemas import TrialCreateRequest

    admin = _admin(db)
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-dup-course")
    body = TrialCreateRequest(
        email="trial-e2e-dup@x.kz", name="Prospect", course_id=c.id,
        lesson_ids=[l1.id], expires_at=_future(), send_invite=True,
    )
    create_trial(body, background_tasks=BackgroundTasks(), db=db, current_user=admin)

    with pytest.raises(HTTPException) as exc:
        create_trial(body, background_tasks=BackgroundTasks(), db=db, current_user=admin)
    assert exc.value.status_code == 409
    assert exc.value.detail == _DUPLICATE_ACTIVE_DETAIL


def test_create_trial_stale_active_window_flips_and_succeeds(db):
    """An 'active'-status grant past its deadline is bookkeeping debt, not a real
    conflict — create_trial must flip it to expired inline and create the new
    grant in the same call, with no IntegrityError from the partial unique index."""
    from src.trials.routes.trials import create_trial
    from src.trials.schemas import TrialCreateRequest
    from src.trials.models import TrialAccess, TRIAL_ACTIVE, TRIAL_EXPIRED

    admin = _admin(db)
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-stale-course")
    email = "trial-e2e-stale@x.kz"
    u = _trial_user(db, email)
    stale = _make_grant(db, u, c, [l1.id], status=TRIAL_ACTIVE, expires_delta=timedelta(days=-1))

    body = TrialCreateRequest(
        email=email, name="Prospect", course_id=c.id, lesson_ids=[l2.id],
        expires_at=_future(), send_invite=False,
    )
    resp = create_trial(body, background_tasks=BackgroundTasks(), db=db, current_user=admin)

    rows = (
        db.query(TrialAccess)
        .filter(TrialAccess.user_id == u.id, TrialAccess.course_id == c.id)
        .order_by(TrialAccess.id)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].id == stale.id
    assert rows[0].status == TRIAL_EXPIRED  # stale row flipped inline
    assert rows[1].status == TRIAL_ACTIVE   # new grant created
    assert resp.trial.id == rows[1].id


def test_update_trial_reactivation_conflict_returns_409(db):
    """Extending an expired grant's deadline while another active grant already
    covers the same (user, course) pair must be rejected, not silently allowed to
    collide with the partial unique index."""
    from src.trials.routes.trials import update_trial, _REACTIVATE_CONFLICT_DETAIL
    from src.trials.schemas import TrialUpdateRequest
    from src.trials.models import TRIAL_ACTIVE, TRIAL_EXPIRED

    admin = _admin(db)
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-reactivate-course")
    u = _trial_user(db, "trial-e2e-reactivate@x.kz")
    expired = _make_grant(db, u, c, [l1.id], status=TRIAL_EXPIRED, expires_delta=timedelta(days=-2))
    _make_grant(db, u, c, [l2.id], status=TRIAL_ACTIVE, expires_delta=timedelta(hours=24))

    body = TrialUpdateRequest(expires_at=_future(hours=48))
    with pytest.raises(HTTPException) as exc:
        update_trial(expired.id, body, db=db, current_user=admin)
    assert exc.value.status_code == 409
    assert exc.value.detail == _REACTIVATE_CONFLICT_DETAIL

    db.refresh(expired)
    assert expired.status == TRIAL_EXPIRED  # rejected before any status flip


def test_update_trial_reactivates_expired_grant_without_conflict(db):
    """Companion to the conflict case above: with no competing active grant,
    extending an expired grant's deadline into the future must actually flip it
    back to active (this is the behavior the conflict branch exists to protect)."""
    from src.trials.routes.trials import update_trial
    from src.trials.schemas import TrialUpdateRequest
    from src.trials.models import TRIAL_ACTIVE, TRIAL_EXPIRED

    admin = _admin(db)
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-reactivate-ok-course")
    u = _trial_user(db, "trial-e2e-reactivate-ok@x.kz")
    expired = _make_grant(db, u, c, [l1.id], status=TRIAL_EXPIRED, expires_delta=timedelta(days=-2))

    body = TrialUpdateRequest(expires_at=_future(hours=48))
    result = update_trial(expired.id, body, db=db, current_user=admin)
    assert result.status == TRIAL_ACTIVE

    db.refresh(expired)
    assert expired.status == TRIAL_ACTIVE


def test_revoke_after_convert_returns_409(db):
    from src.trials.routes.trials import revoke_trial, convert_trial
    from src.trials.models import TRIAL_CONVERTED

    admin = _admin(db)
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-revoke-after-convert")
    u = _trial_user(db, "trial-e2e-rac@x.kz")
    g = _make_grant(db, u, c, [l1.id])

    convert_trial(g.id, db=db, current_user=admin)
    db.refresh(g)
    assert g.status == TRIAL_CONVERTED

    with pytest.raises(HTTPException) as exc:
        revoke_trial(g.id, db=db, current_user=admin)
    assert exc.value.status_code == 409
    assert "converted" in exc.value.detail


def test_convert_after_revoke_returns_409(db):
    from src.trials.routes.trials import revoke_trial, convert_trial
    from src.trials.models import TRIAL_REVOKED

    admin = _admin(db)
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-convert-after-revoke")
    u = _trial_user(db, "trial-e2e-car@x.kz")
    g = _make_grant(db, u, c, [l1.id])

    revoke_trial(g.id, db=db, current_user=admin)
    db.refresh(g)
    assert g.status == TRIAL_REVOKED

    with pytest.raises(HTTPException) as exc:
        convert_trial(g.id, db=db, current_user=admin)
    assert exc.value.status_code == 409
    assert "revoked" in exc.value.detail


def test_create_trial_keeps_password_when_another_active_grant_exists(db):
    """An existing trial user already holding a live grant for a DIFFERENT course
    is actively using their current credentials — creating a second trial for
    them must not rotate the password out from under them."""
    from src.trials.routes.trials import create_trial
    from src.trials.schemas import TrialCreateRequest
    from src.trials.models import TRIAL_ACTIVE

    admin = _admin(db)
    c1, m1, (l1a, l1b) = _course_with_lessons(db, "trial-e2e-pw-retain-course-1")
    c2, m2, (l2a, l2b) = _course_with_lessons(db, "trial-e2e-pw-retain-course-2")
    email = "trial-e2e-pw-retain@x.kz"
    u = _trial_user(db, email, hashed_password="original-hash")
    _make_grant(db, u, c1, [l1a.id], status=TRIAL_ACTIVE, expires_delta=timedelta(hours=24))
    original_hash = u.hashed_password

    body = TrialCreateRequest(
        email=email, name="Prospect", course_id=c2.id, lesson_ids=[l2a.id],
        expires_at=_future(), send_invite=False,
    )
    resp = create_trial(body, background_tasks=BackgroundTasks(), db=db, current_user=admin)

    assert resp.generated_password is None
    db.refresh(u)
    assert u.hashed_password == original_hash


def test_create_trial_rotates_password_when_no_live_grants(db):
    """An existing trial user with no live grants at all (e.g. their only prior
    trial expired/was revoked) is not using their old credentials for anything
    live — a fresh trial rotates the password so the new invite email is valid."""
    from src.trials.routes.trials import create_trial
    from src.trials.schemas import TrialCreateRequest

    admin = _admin(db)
    c, m, (l1, l2) = _course_with_lessons(db, "trial-e2e-pw-rotate-course")
    email = "trial-e2e-pw-rotate@x.kz"
    u = _trial_user(db, email, hashed_password="original-hash")
    original_hash = u.hashed_password

    body = TrialCreateRequest(
        email=email, name="Prospect", course_id=c.id, lesson_ids=[l1.id],
        expires_at=_future(), send_invite=False,
    )
    resp = create_trial(body, background_tasks=BackgroundTasks(), db=db, current_user=admin)

    assert resp.generated_password is not None
    db.refresh(u)
    assert u.hashed_password != original_hash
