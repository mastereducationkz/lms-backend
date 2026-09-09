"""Which rate a lesson is paid at is the CRM's verdict, not a guess made here.

Whether a group is one student's course decides money on both sides: billing charges an
individual lesson at a different rate, payroll pays one at a different rate. The CRM reaches
that verdict from the group's *starting roster* — one student at its first marked lesson, and
never a second one since. This side could only read ``groups.group_type`` and look for «Indi»
in the name, and got two kinds of answer wrong:

* «Indi Inayat & Tomiris SAT 2026» is two named students. The name said individual; the CRM,
  reading the register, said group. The teacher's payslip and the CRM payroll statement then
  quoted different rates for the same lesson.
* A group whose registers say nothing at all has only its name to go on — dropping the name
  guess would have broken those instead.

So the CRM pushes its verdict into ``group_pay_kinds`` and the payslip reads it. A group with
no row keeps the old reading, which is what a group created since the last push looks like.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from src.schemas.models import Attendance, Event, EventGroup, Group, GroupStudent, UserInDB

_DDL = """
CREATE TABLE IF NOT EXISTS group_pay_kinds (
    group_id INTEGER PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
    pay_kind VARCHAR(16) NOT NULL,
    basis VARCHAR(16),
    updated_at TIMESTAMP
)
"""


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

    # The table arrives by migration gpk1_group_pay_kind; a test database built with
    # `create_all` from an older checkout will not have it yet.
    with engine.begin() as ddl:
        ddl.execute(text(_DDL))

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


@pytest.fixture
def world(db):
    stamp = datetime.utcnow().timestamp()
    teacher = UserInDB(email=f"gulzada-{stamp}@test.local", name="Gulzada", role="teacher",
                       hashed_password="x", is_active=True)
    student = UserInDB(email=f"amina-{stamp}@test.local", name="Amina", role="student",
                       hashed_password="x", is_active=True)
    db.add_all([teacher, student])
    db.flush()

    def group(name: str, group_type: str = "group") -> Group:
        g = Group(name=name, teacher_id=teacher.id, is_active=True, group_type=group_type)
        db.add(g)
        db.flush()
        db.add(GroupStudent(group_id=g.id, student_id=student.id,
                            created_at=datetime.utcnow() - timedelta(days=90)))
        db.flush()
        return g

    def taught(g: Group, *, days_ago: int = 2, minutes: int = 60) -> Event:
        when = datetime.utcnow() - timedelta(days=days_ago)
        ev = Event(title=g.name, event_type="class", start_datetime=when,
                   end_datetime=when + timedelta(minutes=minutes),
                   teacher_id=teacher.id, created_by=teacher.id, is_active=True)
        db.add(ev)
        db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=g.id))
        db.add(Attendance(event_id=ev.id, user_id=student.id, status="present"))
        db.flush()
        return ev

    def mirror(g: Group, pay_kind: str, basis: str = "roster") -> None:
        db.execute(
            text(
                "INSERT INTO group_pay_kinds (group_id, pay_kind, basis, updated_at) "
                "VALUES (:g, :k, :b, :t) ON CONFLICT (group_id) DO UPDATE SET "
                "pay_kind = EXCLUDED.pay_kind, basis = EXCLUDED.basis"
            ),
            {"g": g.id, "k": pay_kind, "b": basis, "t": datetime.utcnow()},
        )
        db.flush()

    return {"db": db, "teacher": teacher, "group": group, "taught": taught, "mirror": mirror}


def _salary(db, teacher):
    from src.admin.routes.dashboard import get_teacher_salary_breakdown

    today = datetime.utcnow().date()
    return get_teacher_salary_breakdown(
        period_start=(today - timedelta(days=7)).isoformat(),
        period_end=today.isoformat(),
        lesson_rate=None,
        current_user=teacher,
        db=db,
    )


def test_a_group_the_crm_calls_individual_is_paid_at_the_individual_rate(world):
    """Group 290's shape: one student from the start, and nothing in its name saying so."""
    db, teacher = world["db"], world["teacher"]
    g = world["group"]("Darkhan SAT 2026 - Сырым")
    world["taught"](g)
    world["mirror"](g, "individual")

    body = _salary(db, teacher)

    (row,) = body["groups"]
    assert row["lesson_rate_tenge"] == body["individual_rate"]


def test_a_name_saying_indi_does_not_beat_the_crms_verdict(world):
    """«Indi Inayat & Tomiris SAT 2026» — two named students, and so a group."""
    db, teacher = world["db"], world["teacher"]
    g = world["group"]("Indi Inayat & Tomiris SAT 2026")
    world["taught"](g)
    world["mirror"](g, "group")

    body = _salary(db, teacher)

    (row,) = body["groups"]
    assert row["lesson_rate_tenge"] == body["lesson_rate"], (
        "the name is not evidence when the register is"
    )


def test_a_group_with_no_mirrored_verdict_keeps_the_old_reading(world):
    """Created since the last push. Falling back beats refusing to produce a payslip."""
    db, teacher = world["db"], world["teacher"]
    g = world["group"]("Indi Asya SAT 2026")
    world["taught"](g)

    body = _salary(db, teacher)

    (row,) = body["groups"]
    assert row["lesson_rate_tenge"] == body["individual_rate"]
