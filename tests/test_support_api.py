"""Tests for the read-only /support-api endpoints (X-API-Key service auth).

Auth-dependency tests (503/401/ok) call `verify_support_api_key` directly and
need no DB. The 404 / 200-shape tests run against a real Postgres, using the
same SAVEPOINT-isolation fixture as tests/test_trial_access_db.py and
tests/test_lesson_topic.py (route handlers here don't call db.commit(), but we
still isolate via SAVEPOINT for consistency and because a plain rollback
fixture is the repo convention for these DB-backed tests) — auto-skips if no
local test Postgres is reachable.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.routes import support_api


# ---------------------------------------------------------------------------
# Auth dependency (no DB)
# ---------------------------------------------------------------------------

def test_missing_config_returns_503(monkeypatch):
    monkeypatch.delenv("SUPPORT_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        support_api.verify_support_api_key(api_key="anything")
    assert exc.value.status_code == 503


def test_wrong_key_returns_401(monkeypatch):
    monkeypatch.setenv("SUPPORT_API_KEY", "expected-key")
    with pytest.raises(HTTPException) as exc:
        support_api.verify_support_api_key(api_key="wrong-key")
    assert exc.value.status_code == 401


def test_missing_key_returns_401(monkeypatch):
    monkeypatch.setenv("SUPPORT_API_KEY", "expected-key")
    with pytest.raises(HTTPException) as exc:
        support_api.verify_support_api_key(api_key=None)
    assert exc.value.status_code == 401


def test_correct_key_is_accepted(monkeypatch):
    monkeypatch.setenv("SUPPORT_API_KEY", "expected-key")
    assert support_api.verify_support_api_key(api_key="expected-key") == "expected-key"


# ---------------------------------------------------------------------------
# DB-backed: real Postgres, SAVEPOINT isolation, auto-skip if unreachable
# ---------------------------------------------------------------------------

URL = os.getenv(
    "SUPPORT_API_TEST_DB_URL",
    "postgresql://myuser:mypassword@localhost:5432/lms_test",
)


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
    from src.models.base import Base
    import src.schemas.models  # noqa: F401  (registers every model onto Base.metadata)
    Base.metadata.create_all(bind=ENGINE)


@pytest.fixture()
def db():
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

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


# --- seed helpers ------------------------------------------------------------

def _user(db, email, role="student", name="Test User", **kwargs):
    from src.schemas.models import UserInDB
    u = UserInDB(email=email, name=name, hashed_password="x", role=role, is_active=True, **kwargs)
    db.add(u)
    db.flush()
    return u


def _group(db, name, curator=None, teacher=None, program_type="sat"):
    from src.schemas.models import Group
    g = Group(
        name=name,
        curator_id=curator.id if curator else None,
        teacher_id=teacher.id if teacher else None,
        program_type=program_type,
        is_active=True,
    )
    db.add(g)
    db.flush()
    return g


def _enroll(db, group, student):
    from src.schemas.models import GroupStudent
    gs = GroupStudent(group_id=group.id, student_id=student.id)
    db.add(gs)
    db.flush()
    return gs


def _course_with_summary(db, student, title, completion_pct=42.0, avg_score=88.0):
    from src.schemas.models import Course, Module, Lesson, StudentCourseSummary
    c = Course(title=title, is_active=True)
    db.add(c)
    db.flush()
    m = Module(course_id=c.id, title="M1", order_index=0)
    db.add(m)
    db.flush()
    l = Lesson(module_id=m.id, title="L1", order_index=0)
    db.add(l)
    db.flush()
    summary = StudentCourseSummary(
        user_id=student.id,
        course_id=c.id,
        completion_percentage=completion_pct,
        average_assignment_percentage=avg_score,
    )
    db.add(summary)
    db.flush()
    return c


def _event(db, group, creator, start_delta, title="Lesson"):
    from src.schemas.models import Event, EventGroup
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ev = Event(
        title=title,
        event_type="class",
        start_datetime=now + start_delta,
        end_datetime=now + start_delta + timedelta(hours=1),
        created_by=creator.id,
        is_active=True,
    )
    db.add(ev)
    db.flush()
    eg = EventGroup(event_id=ev.id, group_id=group.id)
    db.add(eg)
    db.flush()
    return ev


def _attendance(db, event, student, status="present"):
    from src.schemas.models import Attendance
    att = Attendance(event_id=event.id, user_id=student.id, status=status)
    db.add(att)
    db.flush()
    return att


def _seed_full_student(db):
    """Seed a student with a group (curator + teacher, mixed-case emails), a
    course-progress summary, past + future events with attendance, and one
    group-scoped graded assignment. Returns (student, group, curator, teacher)."""
    from src.schemas.models import Assignment, AssignmentSubmission

    curator = _user(db, "Curator@Example.com", role="curator", name="Aigerim Curator")
    teacher = _user(db, "Teacher@Example.com", role="teacher", name="Bekzat Teacher")
    admin = _user(db, "admin-support-api-seed@x.kz", role="admin", name="Admin")
    student = _user(db, "student-support-api@x.kz", role="student", name="Student One", student_id="STU-001")

    group = _group(db, "SAT-Support-API-1", curator=curator, teacher=teacher, program_type="sat")
    _enroll(db, group, student)

    _course_with_summary(db, student, "Support API Course", completion_pct=42.0, avg_score=88.0)

    past_event = _event(db, group, admin, timedelta(days=-1), title="Past Lesson")
    _attendance(db, past_event, student, status="present")
    past_event2 = _event(db, group, admin, timedelta(days=-2), title="Past Lesson 2")
    _attendance(db, past_event2, student, status="absent")

    future_event = _event(db, group, admin, timedelta(days=3), title="Upcoming Lesson")

    a1 = Assignment(
        group_id=group.id, title="Essay 1", assignment_type="text",
        content="{}", max_score=100, is_active=True,
    )
    db.add(a1)
    db.flush()
    sub1 = AssignmentSubmission(
        assignment_id=a1.id, user_id=student.id, answers="{}",
        max_score=100, score=90, is_graded=True,
        graded_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(sub1)

    # A second assignment the student has NOT submitted -> counts toward
    # pending_count AND surfaces (with its due date) in homework.pending_items.
    a2 = Assignment(
        group_id=group.id, title="Essay 2 (pending)", assignment_type="text",
        content="{}", max_score=100, is_active=True,
        due_date=datetime(2026, 12, 31, 21, 0, 0),
    )
    db.add(a2)
    db.flush()

    return student, group, curator, teacher, future_event


# --- 404s ---------------------------------------------------------------------

def test_get_user_summary_404_unknown_email(db):
    with pytest.raises(HTTPException) as exc:
        support_api.get_user_summary(email="nobody-support-api@x.kz", db=db)
    assert exc.value.status_code == 404


def test_get_student_context_404_unknown_email(db):
    with pytest.raises(HTTPException) as exc:
        support_api.get_student_context(email="nobody-support-api@x.kz", db=db)
    assert exc.value.status_code == 404


# --- 200 shapes -----------------------------------------------------------------

def test_get_user_summary_shape_and_lowercased_curator_emails(db):
    student, group, curator, teacher, future_event = _seed_full_student(db)

    result = support_api.get_user_summary(email=student.email.upper(), db=db)

    assert result["role"] == "student"
    assert result["full_name"] == "Student One"
    assert result["is_active"] is True
    assert result["curator_emails"] == ["curator@example.com"]


def test_get_student_context_shape(db):
    student, group, curator, teacher, future_event = _seed_full_student(db)

    ctx = support_api.get_student_context(email=student.email, db=db)

    # top-level keys
    for key in ("profile", "groups", "progress", "attendance", "homework", "upcoming_lessons", "trial", "activity"):
        assert key in ctx

    assert ctx["profile"] == {"name": "Student One", "email": student.email.lower(), "student_id": "STU-001"}

    assert len(ctx["groups"]) == 1
    g = ctx["groups"][0]
    assert g["name"] == "SAT-Support-API-1"
    assert g["program_type"] == "sat"
    assert g["curator"] == {"name": "Aigerim Curator", "email": "curator@example.com"}
    assert g["teacher"] == {"name": "Bekzat Teacher", "email": "teacher@example.com"}

    assert ctx["progress"]["courses"] == [
        {"title": "Support API Course", "completion_pct": 42.0, "avg_score": 88.0}
    ]

    assert ctx["attendance"]["rate_pct"] == 50.0
    assert len(ctx["attendance"]["recent"]) == 2
    assert {r["status"] for r in ctx["attendance"]["recent"]} == {"attended", "missed"}

    assert ctx["homework"]["pending_count"] == 1
    assert ctx["homework"]["recent_grades"] == [{"title": "Essay 1", "grade": 90}]
    # pending_items lists the OUTSTANDING assignments with their due dates so the
    # Support assistant can name them (Essay 1 is submitted, so only Essay 2 shows).
    assert ctx["homework"]["pending_items"] == [
        {"title": "Essay 2 (pending)", "due_at": "2026-12-31T21:00:00"}
    ]

    assert len(ctx["upcoming_lessons"]) == 1
    assert ctx["upcoming_lessons"][0]["title"] == "Upcoming Lesson"

    assert ctx["trial"] == {"is_trial": False, "expires_at": None}

    assert ctx["activity"] == {"streak": 0, "last_active": None}


def test_get_student_context_empty_groups_returns_defaults(db):
    student = _user(db, "lonely-student-support-api@x.kz", role="student", name="Lonely Student")

    ctx = support_api.get_student_context(email=student.email, db=db)

    assert ctx["groups"] == []
    assert ctx["progress"]["courses"] == []
    assert ctx["attendance"] == {"rate_pct": None, "recent": []}
    assert ctx["homework"] == {"pending_count": 0, "pending_items": [], "recent_grades": []}
    assert ctx["upcoming_lessons"] == []
