"""The IELTS date check-in must not ask for a date we already hold.

The banner claimed "We do not have your exact IELTS date yet" while the countdown
directly beneath it displayed that very date, because the check-in only ever consulted
the 14-day cadence and never looked at the stored date.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.schemas.models import (  # noqa: F401  (import-order guard)
    AssignmentZeroSubmission,
    Group,
    GroupStudent,
    UserInDB,
)
from src.assignments.routes.assignment_zero import get_ielts_date_prompt_status


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


@pytest.fixture
def ielts_student(db):
    student = UserInDB(email="ip-stu@t.io", name="IP Student", hashed_password="x",
                       role="student", is_active=True)
    db.add(student)
    db.flush()
    group = Group(name="ip Bonus Group IELTS", is_active=True, is_over=False,
                  program_type="ielts")
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()
    return student


def _submission(db, student, **kw):
    row = AssignmentZeroSubmission(
        user_id=student.id, full_name=student.name, phone_number="",
        parent_phone_number="", telegram_id="", email=student.email,
        college_board_email="", college_board_password="", birthday_date=None,
        city="", school_type="", group_name="", sat_target_date="",
        recent_practice_test_score="", bluebook_practice_test_5_score="", **kw,
    )
    db.add(row)
    db.flush()
    return row


def test_no_prompt_when_a_future_date_is_already_known(db, ielts_student):
    """REGRESSION: the banner contradicted the countdown showing the same date."""
    _submission(db, ielts_student,
                ielts_planned_test_date=date.today() + timedelta(days=30))
    out = get_ielts_date_prompt_status(current_user=ielts_student, db=db)
    assert out["is_ielts_student"] is True
    assert out["should_prompt"] is False
    assert out["has_exact_date"] is True
    assert out["reason"] == "exact_date_known"


def test_a_date_today_still_counts_as_known(db, ielts_student):
    _submission(db, ielts_student, ielts_planned_test_date=date.today())
    assert get_ielts_date_prompt_status(current_user=ielts_student, db=db)["should_prompt"] is False


def test_prompts_when_no_date_is_stored(db, ielts_student):
    _submission(db, ielts_student, ielts_target_score="7.0")
    out = get_ielts_date_prompt_status(current_user=ielts_student, db=db)
    assert out["should_prompt"] is True
    assert out["has_exact_date"] is False
    assert out["planned_test_date"] is None


def test_prompts_again_once_the_date_has_passed(db, ielts_student):
    """The student has either sat the exam or rebooked; both are worth asking about."""
    past = date.today() - timedelta(days=3)
    _submission(db, ielts_student, ielts_planned_test_date=past)
    out = get_ielts_date_prompt_status(current_user=ielts_student, db=db)
    assert out["should_prompt"] is True
    assert out["date_has_passed"] is True
    # The stored date is returned so the UI says "that date has passed" rather than
    # "we do not have your date".
    assert out["planned_test_date"] == past.isoformat()


def test_a_known_future_date_beats_the_two_week_cadence(db, ielts_student):
    """Even when the cadence is due, there is nothing to ask if we know the date."""
    _submission(db, ielts_student,
                ielts_planned_test_date=date.today() + timedelta(days=60),
                ielts_last_date_prompted_at=datetime.now(timezone.utc) - timedelta(days=90))
    assert get_ielts_date_prompt_status(current_user=ielts_student, db=db)["should_prompt"] is False


def test_non_ielts_student_is_never_prompted(db):
    student = UserInDB(email="ip-sat@t.io", name="SAT Only", hashed_password="x",
                       role="student", is_active=True)
    db.add(student)
    db.flush()
    group = Group(name="ip SAT group", is_active=True, is_over=False, program_type="sat")
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()

    out = get_ielts_date_prompt_status(current_user=student, db=db)
    assert out["is_ielts_student"] is False
    assert out["should_prompt"] is False
