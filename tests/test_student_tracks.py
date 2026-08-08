"""One rule for "which exam tracks is this student on?".

A student on both SAT and IELTS saw two countdowns but only one platform tile: the
countdown and the tiles each ran their own group query with different filters. These
tests pin the shared resolver AND assert the two features now agree, because agreement
is the actual requirement - either answer alone can look reasonable in isolation.
"""
from datetime import date, timedelta

import pytest

from src.schemas.models import (  # noqa: F401  (import-order guard)
    AssignmentZeroSubmission,
    Group,
    GroupStudent,
    UserInDB,
)
from src.assignments.routes.assignment_zero import get_exam_countdown
from src.exams.tracks import resolve_student_tracks


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


def _student(db, email="tr-stu@t.io"):
    u = UserInDB(email=email, name="TR Student", hashed_password="x",
                 role="student", is_active=True)
    db.add(u)
    db.flush()
    return u


def _join(db, student, name, *, program_type="general_english",
          is_active=True, is_over=False, is_special=False):
    g = Group(name=name, program_type=program_type, is_active=is_active,
              is_over=is_over, is_special=is_special)
    db.add(g)
    db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id))
    db.flush()
    return g


def _az(db, student, **kw):
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


# --------------------------------------------------------------------------------------
# The reported bug
# --------------------------------------------------------------------------------------

def test_a_student_on_both_tracks_gets_both(db):
    student = _student(db)
    _join(db, student, "tr SAT group", program_type="sat")
    _join(db, student, "tr IELTS group", program_type="ielts")
    assert resolve_student_tracks(db, student) == ["sat", "ielts"]


def test_countdown_and_tiles_agree_on_the_same_student(db):
    """The heart of the bug: two countdowns, one tile. They must not disagree."""
    student = _student(db)
    _join(db, student, "tr SAT group", program_type="sat")
    _join(db, student, "tr IELTS group", program_type="ielts")

    tiles = resolve_student_tracks(db, student)
    countdown = get_exam_countdown(current_user=student, db=db)["available_exams"]

    # NUET is the only track the countdown deliberately omits (no official dates yet).
    assert set(countdown) == {t for t in tiles if t != "nuet"}


def test_an_upcoming_date_keeps_the_track_after_the_group_ends(db):
    """Someone sitting SAT in December is still a SAT student when the group closes."""
    student = _student(db)
    _join(db, student, "tr IELTS group", program_type="ielts")
    _join(db, student, "tr old SAT group", program_type="sat", is_over=True)
    _az(db, student, sat_planned_test_date=date.today() + timedelta(days=90))

    assert resolve_student_tracks(db, student) == ["sat", "ielts"]


def test_an_ended_group_alone_does_not_grant_a_track(db):
    student = _student(db)
    _join(db, student, "tr finished SAT", program_type="sat", is_over=True)
    assert resolve_student_tracks(db, student) == []


def test_a_past_date_alone_does_not_keep_the_track(db):
    student = _student(db)
    _join(db, student, "tr finished SAT", program_type="sat", is_over=True)
    _az(db, student, sat_planned_test_date=date.today() - timedelta(days=10))
    assert resolve_student_tracks(db, student) == []


# --------------------------------------------------------------------------------------
# Matching rules
# --------------------------------------------------------------------------------------

def test_saturday_is_not_read_as_sat(db):
    """The countdown's old substring match made "Saturday" a SAT group."""
    student = _student(db)
    _join(db, student, "tr Saturday English club", program_type="general_english")
    assert resolve_student_tracks(db, student) == []


def test_a_legacy_group_is_matched_by_name_when_program_type_was_never_set(db):
    """NUET was never backfilled, so those groups still say general_english."""
    student = _student(db)
    _join(db, student, "tr NUET intensive", program_type="general_english")
    assert resolve_student_tracks(db, student) == ["nuet"]


def test_the_recurring_ielts_misspelling_still_matches(db):
    student = _student(db)
    _join(db, student, "tr IEALTS evening", program_type="general_english")
    assert resolve_student_tracks(db, student) == ["ielts"]


def test_special_groups_are_not_tracks(db):
    student = _student(db)
    _join(db, student, "tr SAT special", program_type="sat", is_special=True)
    assert resolve_student_tracks(db, student) == []


def test_general_english_is_not_a_track(db):
    student = _student(db)
    _join(db, student, "tr General English A2", program_type="general_english")
    assert resolve_student_tracks(db, student) == []


def test_tracks_are_deduplicated_and_ordered(db):
    student = _student(db)
    _join(db, student, "tr SAT one", program_type="sat")
    _join(db, student, "tr SAT two", program_type="sat")
    _join(db, student, "tr IELTS one", program_type="ielts")
    _join(db, student, "tr NUET one", program_type="nuet")
    assert resolve_student_tracks(db, student) == ["sat", "nuet", "ielts"]
